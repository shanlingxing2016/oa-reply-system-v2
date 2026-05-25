from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import json
import re
from database import get_db
from models import Case, Document, GeneratedDocument, Comparison
from services.ai_service import AIService
from config import DEEPSEEK_API_KEY

router = APIRouter(prefix="/api/cases/{case_id}", tags=["analysis"])


class AnalyzeRequest(BaseModel):
    oa_document_id: Optional[int] = None


class GenerateDocRequest(BaseModel):
    strategy: Optional[str] = "S3"
    agent_notes: Optional[str] = ""
    template_text: Optional[str] = ""
    tech_problem: Optional[str] = ""  # 前端确认框中代理人确认的技术问题
    table2_features: Optional[list] = None  # 前端确认的区别特征列表（优先于DB）


def _fuzzy_match_t1(feature_text: str, t1_map: dict):
    """【修复5】模糊匹配表二特征在表一中的对应行。
    用户可能编辑过表二中的特征文字，导致与表一原始特征不完全一致。
    使用子串包含 + 重叠度评分来匹配。"""
    if not feature_text or not t1_map:
        return None
    f = feature_text.strip()
    best = None
    best_score = 0.0
    for t1_feat, t1_row in t1_map.items():
        t1f = t1_feat.strip()
        if not t1f:
            continue
        shorter = f if len(f) <= len(t1f) else t1f
        longer = t1f if len(f) <= len(t1f) else f
        if len(shorter) > 0 and shorter in longer:
            score = len(shorter) / len(longer)
            if score > best_score and score > 0.3:
                best_score = score
                best = t1_row
    return best


def _strip_feature_from_arg(argument: str, original_feature: str) -> str:
    """从 AI 论证段落中剥离被 AI 复述/改写/重复的特征文字。
    AI 经常在论证段落开头重复区别特征描述（且会改写），
    导致意见陈述书中出现与表二不一致的特征内容。
    """
    if not argument or not original_feature:
        return argument or ""

    arg = argument.strip()
    feat = original_feature.strip()

    # 阈值：特征少于20字时不剥离（太短容易误伤）
    if len(feat) < 20:
        return arg

    # 策略1：精确包含 — 如果论证开头包含原始特征，直接删除
    if feat in arg:
        arg = arg.replace(feat, "")

    # 策略2：基于关键词的重叠检测 — 提取特征中的关键短语，在论证中查找并剥离
    # 将特征按标点分割为短语，找论证中是否包含这些短语簇
    import re
    phrases = re.split(r'[，。；：；、\n]', feat)
    phrases = [p.strip() for p in phrases if len(p.strip()) > 10]

    if phrases:
        # 如果论证开头前200字内包含超过60%的关键短语，视为AI在复述特征
        head = arg[:min(400, len(arg))]
        matched = sum(1 for p in phrases if p in head)
        if len(phrases) >= 2 and matched >= len(phrases) * 0.6:
            # 尝试移除包含这些短语的连续段落
            # 从开头找到最后一个匹配短语的位置，截断之前的内容
            last_pos = 0
            for p in phrases:
                idx = arg.find(p)
                if idx >= 0:
                    end = idx + len(p)
                    if end > last_pos:
                        last_pos = end

            if last_pos > len(arg) * 0.2 and last_pos < len(arg) * 0.8:
                arg = arg[last_pos:]

            # 移除常见AI衔接词头
            arg = re.sub(
                r'^[\s\n]*(该区别(技术)?特征\s*(涉及|是指|在于|为|具体为|的内容为)[^。\n]*[。\n]?)',
                '', arg, flags=re.IGNORECASE
            )
            arg = re.sub(
                r'^[\s\n]*(区别特征[①②③④⑤⑥⑦⑧⑨⑩]*\s*[：:].*?)(?=\n\s*\n|$)',
                '', arg, flags=re.IGNORECASE
            )
            arg = re.sub(
                r'^[\s\n]*(权利要求\d+\s*中的\s*上述\s*区别\s*技术\s*特征\s*[^。\n]*[。\n]?)',
                '', arg, flags=re.IGNORECASE
            )

    # 清理
    arg = arg.strip()
    arg = re.sub(r'\n{3,}', '\n\n', arg)  # 合并多余空行
    return arg


def _strip_html(text: str) -> str:
    """去除 HTML 标签，仅保留纯文本（用于模板构建时清理特征文字中的红色标记 span）"""
    if not text:
        return ""
    return re.sub(r'<[^>]+>', '', text)


def _build_opinion_template(case_id: int, db: Session, table2_override: list = None):
    """构建意见陈述书模板 — 基于真实行业标准格式。
    
    参考两份真实意见陈述书（维生素A检测、儿茶酚胺检测）的结构：
    一、修改说明
    二、关于权利要求1创造性的意见陈述
        区别特征列出（仅与D1对比的结果，不单独写D1/D2分析段）
        针对上述区别技术特征，本申请实际解决的技术问题是：...
        完整创造性论证（三步法 + D2在论证内讨论）
        技术效果列表
    结论
    
    特征文字从DB直接取值，AI生成完整创造性论证段落。
    若 table2_override 有值，优先使用（前端确认后的数据）。
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    comps = db.query(Comparison).filter(Comparison.case_id == case_id).order_by(Comparison.sort_order).all()

    # 技术问题：agent_notes 存的内容就是技术问题（去掉"技术问题："前缀如有）
    tech_problem = ""
    if case and case.agent_notes:
        raw = case.agent_notes.strip()
        # 去掉可能存在的"技术问题："或"技术问题:"前缀
        tech_problem = re.sub(r'^技术问题[：:]\s*', '', raw)

    # 表二区别特征 — 优先用前端确认数据，否则从DB取
    if table2_override and len(table2_override) > 0:
        # 用前端确认数据构建 table2_features 列表（模拟DB对象结构）
        class _FakeT2:
            pass
        table2_features = []
        for item in table2_override:
            f = _FakeT2()
            f.diff_no = item.get("diff_no", "")
            f.feature = item.get("feature", "")
            f.ref_document = item.get("ref_document", "D2")
            f.ref_position = item.get("ref_position", "")
            f.ref_content = item.get("ref_content", "")
            f.pub_status = item.get("pub_status", "no")
            f.analysis = item.get("analysis", "")
            table2_features.append(f)
    else:
        table2_features = [c for c in comps if c.table_type == "table2" and c.feature]
    # 效果表
    effects = [c for c in comps if c.table_type == "effect" and c.feature]
    # 表一索引（供AI上下文）
    t1_by_feature = {}
    for c in comps:
        if c.table_type == "table1" and c.feature:
            t1_by_feature[c.feature.strip()] = c

    # ---- 构建固定框架 ----
    header = f"""意见陈述书

申请号：{case.case_number if case else '未知'}
发明名称：{case.case_name if case else '未知'}

尊敬的审查员：

"""

    # 一、修改说明
    modification_template = """一、修改说明

__MODIFICATION__

"""

    # 二、创造性意见陈述
    d2_ref_global = "D2"
    if table2_features:
        d2_ref_global = table2_features[0].ref_document or "D2"

    if table2_features:
        creativity_header = f"""二、关于权利要求1创造性的意见陈述

审查员在审查意见通知书中指出，权利要求1相对于对比文件1（D1）和对比文件2（{d2_ref_global}）不具备创造性。

本申请权利要求1与D1相比，区别技术特征如下：

"""
    else:
        creativity_header = "二、关于创造性的意见陈述\n\n"

    # 仅列出区别特征（不写D1/D2分析段，这些信息在AI论证中讨论）
    feature_lines = []
    creativity_context = []
    has_features = False

    for i, t2 in enumerate(table2_features):
        has_features = True
        diff_no = t2.diff_no or f"({i+1})"
        feature_text = _strip_html(t2.feature).strip() if t2.feature else ""
        feature_lines.append(f"区别特征<{diff_no}>：\n{feature_text}\n")

        # D1信息（仅给AI上下文）
        t1 = _fuzzy_match_t1(feature_text, t1_by_feature)
        t1_d1_info = ""
        if t1 and t1.ref_content:
            pos = t1.ref_position or "未知"
            t1_d1_info = f"D1（{pos}）公开了：{t1.ref_content.strip()[:300]}"

        # D2信息（仅给AI上下文，在论证中讨论）
        d2_pub = {"yes": "已公开", "part": "部分公开", "no": "未公开"}.get(t2.pub_status, "未确认")
        d2_ref = t2.ref_document or "D2"
        d2_pos = f"{d2_ref}第{t2.ref_position}" if t2.ref_position else d2_ref
        d2_content = t2.ref_content.strip()[:300] if t2.ref_content else ""
        d2_analysis = t2.analysis.strip()[:300] if t2.analysis else ""

        creativity_context.append({
            "index": i + 1,
            "diff_no": diff_no,
            "feature": feature_text,
            "d1_info": t1_d1_info,
            "d2_doc": d2_ref,
            "d2_pub": d2_pub,
            "d2_position": d2_pos,
            "d2_content": d2_content,
            "d2_analysis": d2_analysis,
        })

    # 技术问题 + 完整创造性论证（一个占位符，AI生成完整段落，D2在论证中讨论）
    creativity_section = ""
    if has_features:
        creativity_section = f"""针对上述区别技术特征，本申请实际解决的技术问题是：**{tech_problem or '（待确定）'}**

__CREATIVE_ARGUMENT__

"""
    else:
        creativity_section = "__CREATIVITY__\n\n"

    # 技术效果（如同参考案例中的效果对比表）
    effect_lines = []
    for e in effects:
        line = f"- {e.feature.strip()}"
        if e.app_value:
            line += f"\n  本申请：{e.app_value}"
        if e.ref_value:
            line += f"\n  {e.ref_document or 'D1'}：{e.ref_value}"
        if e.analysis:
            line += f"\n  分析：{e.analysis.strip()[:200]}"
        effect_lines.append(line)

    effect_section = ""
    if effect_lines:
        effect_section = f"""技术效果：

{chr(10).join(effect_lines)}

__EFFECT_ARGUMENT__

"""

    # 结论
    conclusion_section = """综上所述，__CONCLUSION__

"""

    # 拼接完整固定框架
    fixed_template = (
        header
        + modification_template
        + creativity_header
        + "\n".join(feature_lines)
        + "\n"
        + creativity_section
        + effect_section
        + conclusion_section
    )

    return {
        "fixed_template": fixed_template,
        "tech_problem": tech_problem,
        "creativity_context": creativity_context,
        "effects": [
            {
                "feature": e.feature,
                "app_value": e.app_value,
                "ref_document": e.ref_document,
                "ref_value": e.ref_value,
                "analysis": e.analysis,
            }
            for e in effects
        ],
        "has_features": has_features,
    }


@router.post("/analyze-oa")
async def analyze_oa(case_id: int, body: AnalyzeRequest, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 获取 OA 通知书文本
    oa_doc = None
    if body.oa_document_id:
        oa_doc = db.query(Document).filter(Document.id == body.oa_document_id).first()
    if not oa_doc:
        oa_doc = (
            db.query(Document)
            .filter(Document.case_id == case_id, Document.doc_type == "oa")
            .first()
        )
    if not oa_doc or not oa_doc.extracted_text:
        raise HTTPException(status_code=400, detail="未找到 OA 通知书文本，请先上传并解析")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your-deepseek-api-key-here":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key，请在 .env 文件中设置 DEEPSEEK_API_KEY")

    try:
        ai = AIService()
        result = await ai.analyze_oa(oa_doc.extracted_text)
        case.rejection_reasons = json.dumps(result.get("rejection_reasons", []), ensure_ascii=False)
        case.ai_summary = result.get("summary", "")
        db.commit()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 分析失败: {str(e)}")


@router.post("/suggest-strategies")
async def suggest_strategies(case_id: int, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 收集所有文档文本
    docs = db.query(Document).filter(Document.case_id == case_id).all()
    doc_texts = {}
    for d in docs:
        doc_texts[d.doc_type] = d.extracted_text or ""

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your-deepseek-api-key-here":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    try:
        ai = AIService()
        case_data = {
            "case_number": case.case_number,
            "case_name": case.case_name,
            "rejection_reasons": case.rejection_reasons,
            "oa_text": doc_texts.get("oa", ""),
            "patent_text": doc_texts.get("patent", ""),
            "d1_text": doc_texts.get("d1", ""),
            "d2_text": doc_texts.get("d2", ""),
        }
        result = await ai.suggest_strategies(case_data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 策略推荐失败: {str(e)}")


@router.post("/generate-doc")
async def generate_doc(case_id: int, body: GenerateDocRequest, db: Session = Depends(get_db)):
    """方案A：模板预填充方式生成意见陈述书。
    固定框架从数据库构建，AI只负责论证段落，Python组装最终文档。
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your-deepseek-api-key-here":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    docs = db.query(Document).filter(Document.case_id == case_id).all()
    doc_texts = {}
    for d in docs:
        doc_texts[d.doc_type] = d.extracted_text or ""

    try:
        # 1. 构建固定模板（优先用前端确认的区别特征）
        table2_override = body.table2_features if body.table2_features and len(body.table2_features) > 0 else None
        template_data = _build_opinion_template(case_id, db, table2_override=table2_override)
        fixed_template = template_data["fixed_template"]

        # 若前端确认框传了技术问题，优先使用（代理人已确认的值）
        effective_tech_problem = (body.tech_problem or "").strip() or template_data["tech_problem"]

        # 2. AI 生成论证段落
        ai = AIService()
        ai_raw = await ai.generate_opinion_letter(
            case_number=case.case_number,
            case_name=case.case_name,
            rejection_reasons=case.rejection_reasons,
            oa_text=doc_texts.get("oa", ""),
            patent_text=doc_texts.get("patent", ""),
            d1_text=doc_texts.get("d1", ""),
            d2_text=doc_texts.get("d2", ""),
            strategy=body.strategy,
            agent_notes=body.agent_notes,
            template_text=body.template_text,
            creativity_context=template_data["creativity_context"],
            tech_problem=effective_tech_problem,
            effects=template_data["effects"],
        )

        # 3. 解析 AI 返回的 JSON 论证数据
        import re
        ai_text = ai_raw
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', ai_text)
        if m:
            ai_text = m.group(1).strip()
        else:
            ai_text = ai_text.strip()

        try:
            args_data = json.loads(ai_text)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON 对象
            m = re.search(r'\{[\s\S]*\}', ai_text)
            if m:
                args_data = json.loads(m.group())
            else:
                # 回退：如果AI没返回JSON，用原始文本作为完整文档
                args_data = {}

        # 4. 组装最终文档：将AI生成的段落填入模板占位符
        final_doc = fixed_template

        # 修改说明
        final_doc = final_doc.replace("__MODIFICATION__", args_data.get("modification", "") or "")

        # 完整创造性论证（一个段落，AI按三步法+含D2分析生成）
        creative_arg = args_data.get("creative_argument", "") or ""
        final_doc = final_doc.replace("__CREATIVE_ARGUMENT__", creative_arg)

        # 其它段落
        final_doc = final_doc.replace("__EFFECT_ARGUMENT__", args_data.get("effect_argument", "") or "")
        final_doc = final_doc.replace("__CONCLUSION__", args_data.get("conclusion", "") or "")

        # 清理所有未替换的占位符
        final_doc = re.sub(r'__[A-Z_]+__', '', final_doc)

        gen = GeneratedDocument(
            case_id=case_id,
            doc_content=final_doc,
            strategy_used=body.strategy,
        )
        db.add(gen)
        db.commit()
        db.refresh(gen)
        return {"id": gen.id, "doc_content": final_doc}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档生成失败: {str(e)}")


class ReviseDocRequest(BaseModel):
    current_doc: str
    instructions: str


@router.post("/revise-doc")
async def revise_doc(case_id: int, body: ReviseDocRequest, db: Session = Depends(get_db)):
    """方案A：调整意见陈述书。当前文档已有正确的固定内容，AI只调整论证部分。"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your-deepseek-api-key-here":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    try:
        template_data = _build_opinion_template(case_id, db)

        ai = AIService()
        result = await ai.revise_opinion_letter(
            current_doc=body.current_doc,
            instructions=body.instructions,
            creativity_context=template_data["creativity_context"],
            tech_problem=template_data["tech_problem"],
        )

        gen = GeneratedDocument(
            case_id=case_id,
            doc_content=result,
            strategy_used="revised",
        )
        db.add(gen)
        db.commit()
        db.refresh(gen)
        return {"id": gen.id, "doc_content": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档调整失败: {str(e)}")

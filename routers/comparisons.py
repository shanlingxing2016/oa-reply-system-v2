from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import Case, Comparison, Document
from services.ai_service import AIService
from services.pdf_parser import PDFParser
from config import DEEPSEEK_API_KEY

router = APIRouter(prefix="/api/cases/{case_id}/comparisons", tags=["comparisons"])


class ComparisonCreate(BaseModel):
    table_type: str  # table1/table2/effect
    claim: Optional[str] = None
    feature: Optional[str] = None
    ref_position: Optional[str] = None
    ref_content: Optional[str] = None
    pub_status: Optional[str] = "no"
    analysis: Optional[str] = None
    diff_no: Optional[str] = None
    ref_document: Optional[str] = None
    app_position: Optional[str] = None
    sort_order: Optional[int] = None


class ComparisonUpdate(BaseModel):
    id: int
    claim: Optional[str] = None
    feature: Optional[str] = None
    ref_position: Optional[str] = None
    ref_content: Optional[str] = None
    pub_status: Optional[str] = None
    analysis: Optional[str] = None
    diff_no: Optional[str] = None
    ref_document: Optional[str] = None
    app_position: Optional[str] = None
    sort_order: Optional[int] = None


class BatchSaveRequest(BaseModel):
    table_type: str
    rows: List[ComparisonCreate]


@router.get("")
def get_comparisons(case_id: int, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    result = {}
    for tt in ["table1", "table2", "effect"]:
        rows = (
            db.query(Comparison)
            .filter(Comparison.case_id == case_id, Comparison.table_type == tt)
            .order_by(Comparison.sort_order, Comparison.id)
            .all()
        )
        result[tt] = [
            {
                "id": r.id,
                "claim": r.claim,
                "feature": r.feature,
                "ref_position": r.ref_position,
                "ref_content": r.ref_content,
                "pub_status": r.pub_status,
                "analysis": r.analysis,
                "diff_no": r.diff_no,
                "ref_document": r.ref_document,
                "app_position": r.app_position,
                "app_value": r.app_value,
                "ref_value": r.ref_value,
                "sort_order": r.sort_order,
            }
            for r in rows
        ]
    return result


@router.post("/row")
def create_comparison_row(case_id: int, body: ComparisonCreate, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    row = Comparison(case_id=case_id, **body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "message": "已添加"}


@router.put("/row")
def update_comparison_row(case_id: int, body: ComparisonUpdate, db: Session = Depends(get_db)):
    row = db.query(Comparison).filter(Comparison.id == body.id, Comparison.case_id == case_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    update_data = body.model_dump(exclude={"id"}, exclude_unset=True)
    for k, v in update_data.items():
        setattr(row, k, v)
    db.commit()
    return {"id": row.id, "message": "已更新"}


@router.delete("/row/{row_id}")
def delete_comparison_row(case_id: int, row_id: int, db: Session = Depends(get_db)):
    row = db.query(Comparison).filter(Comparison.id == row_id, Comparison.case_id == case_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(row)
    db.commit()
    return {"message": "已删除"}


@router.post("/batch")
def batch_save_comparisons(case_id: int, body: BatchSaveRequest, db: Session = Depends(get_db)):
    """批量保存某张比对表的所有行（先删后插）"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    # 删除该表类型的所有现有行
    db.query(Comparison).filter(
        Comparison.case_id == case_id, Comparison.table_type == body.table_type
    ).delete()

    # 批量插入
    for i, row_data in enumerate(body.rows):
        row = Comparison(case_id=case_id, **row_data.model_dump(), sort_order=i)
        db.add(row)

    db.commit()
    return {"message": f"已保存 {len(body.rows)} 条记录"}


@router.post("/ai-analyze")
async def ai_analyze(case_id: int, db: Session = Depends(get_db)):
    """AI 自动分析并填写三张比对表"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    # 获取所有文档文本（未解析的按需解析）
    docs = db.query(Document).filter(Document.case_id == case_id).all()
    pdf_parser = PDFParser()
    doc_texts = {}
    parse_errors = {}
    for d in docs:
        text = d.extracted_text or ""
        if not text and d.stored_path:
            try:
                result = pdf_parser.parse(d.stored_path)
                text = result.get("full_text", "")
                if text:
                    d.extracted_text = text
                    db.commit()
                else:
                    parse_errors[d.doc_type] = f"{d.original_filename} 解析结果为空"
            except Exception as e:
                parse_errors[d.doc_type] = f"{d.original_filename} 解析失败: {str(e)[:100]}"
        doc_texts[d.doc_type] = text

    patent_text = doc_texts.get("patent", "")
    d1_text = doc_texts.get("d1", "")
    d2_text = doc_texts.get("d2", "")
    oa_text = doc_texts.get("oa", "")

    if not patent_text or not d1_text:
        # 给出具体哪个文件缺失或解析失败
        missing = []
        if not patent_text:
            err = parse_errors.get("patent", "")
            missing.append("本申请文件" + ("（"+err+"）" if err else "：未上传或未解析"))
        if not d1_text:
            err = parse_errors.get("d1", "")
            missing.append("D1对比文件" + ("（"+err+"）" if err else "：未上传或未解析"))
        raise HTTPException(status_code=400, detail="；".join(missing))

    try:
        ai = AIService()
        result = await ai.analyze_comparisons(patent_text, d1_text, d2_text, oa_text)

        # 保存表一
        if "table1" in result and result["table1"]:
            db.query(Comparison).filter(
                Comparison.case_id == case_id, Comparison.table_type == "table1"
            ).delete()
            for i, row in enumerate(result["table1"]):
                db.add(Comparison(
                    case_id=case_id, table_type="table1",
                    sort_order=i,
                    claim=row.get("claim", ""),
                    feature=row.get("feature", ""),
                    ref_position=row.get("ref_position", ""),
                    ref_content=row.get("ref_content", ""),
                    pub_status=row.get("pub_status", "no"),
                    analysis=row.get("analysis", ""),
                ))

        # 保存技术问题
        if "tech_problem" in result and result["tech_problem"]:
            case.agent_notes = (case.agent_notes or "") + "\n技术问题：" + result["tech_problem"]

        # ---- 表二：从表一提取未公开特征，与 AI 分析合并 ----
        # 1. 从表一中收集 pub_status 为 "no" 或 "part" 的区别特征
        t1_rows = result.get("table1", [])
        diff_features = []
        for r in t1_rows:
            ps = r.get("pub_status", "").strip()
            if ps in ("no", "part"):
                diff_features.append({
                    "claim": r.get("claim", ""),
                    "feature": r.get("feature", ""),
                })

        # 2. AI 原始表二数据（用于匹配填充分析内容）
        ai_table2 = result.get("table2", [])

        # 3. 简单匹配函数：基于特征文本重叠度匹配
        def _match_feature(feature_text, ai_rows):
            if not feature_text:
                return None
            best = None
            best_score = 0
            f = feature_text.strip()
            for row in ai_rows:
                af = row.get("feature", "").strip()
                if not af:
                    continue
                # 计算双向包含度
                shorter = f if len(f) <= len(af) else af
                longer = af if len(f) <= len(af) else f
                if len(shorter) > 0 and shorter in longer:
                    score = len(shorter) / len(longer)
                    if score > best_score:
                        best_score = score
                        best = row
            return best

        # 4. 构建最终表二（特征来自表一，其他字段优先用匹配的 AI 数据）
        db.query(Comparison).filter(
            Comparison.case_id == case_id, Comparison.table_type == "table2"
        ).delete()
        circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
        for i, feat in enumerate(diff_features):
            matched = _match_feature(feat["feature"], ai_table2)
            diff_no = circled[i] if i < len(circled) else str(i + 1)
            db.add(Comparison(
                case_id=case_id, table_type="table2",
                sort_order=i,
                diff_no=diff_no,
                feature=feat["feature"],  # 强制使用表一的特征
                ref_document=matched.get("ref_document", "D2") if matched else "D2",
                ref_position=matched.get("ref_position", "") if matched else "",
                ref_content=matched.get("ref_content", "") if matched else "",
                pub_status=matched.get("pub_status", "no") if matched else "no",
                analysis=matched.get("analysis", "") if matched else "",
            ))

        # 保存效果表
        if "effect" in result and result["effect"]:
            db.query(Comparison).filter(
                Comparison.case_id == case_id, Comparison.table_type == "effect"
            ).delete()
            for i, row in enumerate(result["effect"]):
                db.add(Comparison(
                    case_id=case_id, table_type="effect",
                    sort_order=i,
                    feature=row.get("feature", ""),
                    app_position=row.get("app_position", ""),
                    app_value=row.get("app_value", ""),
                    ref_document=row.get("ref_document", "D1"),
                    ref_position=row.get("ref_position", ""),
                    ref_value=row.get("ref_value", ""),
                    ref_content=row.get("ref_content", ""),
                    pub_status=row.get("pub_status", "no"),
                    analysis=row.get("analysis", ""),
                ))

        db.commit()
        return {"message": "AI 分析完成，比对表已自动填写", "result": result}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 分析失败：{str(e)}")


@router.post("/ai-analyze-table2")
async def ai_analyze_table2(case_id: int, db: Session = Depends(get_db)):
    """AI 独立分析表二：验证区别技术特征是否被 D2/D3 公开"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    # 获取当前表二的特征列表
    table2_rows = (
        db.query(Comparison)
        .filter(Comparison.case_id == case_id, Comparison.table_type == "table2")
        .order_by(Comparison.sort_order)
        .all()
    )

    if not table2_rows:
        # 如果表二为空，从表一提取区别特征临时构建
        t1_rows = (
            db.query(Comparison)
            .filter(Comparison.case_id == case_id, Comparison.table_type == "table1")
            .order_by(Comparison.sort_order)
            .all()
        )
        diff_features = []
        circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
        idx = 0
        for r in t1_rows:
            if r.pub_status in ("no", "part"):
                diff_features.append({
                    "diff_no": circled[idx] if idx < len(circled) else str(idx + 1),
                    "feature": r.feature or "",
                })
                idx += 1
    else:
        diff_features = [
            {
                "diff_no": r.diff_no or "",
                "feature": r.feature or "",
            }
            for r in table2_rows
        ]

    if not diff_features:
        raise HTTPException(status_code=400, detail="没有区别技术特征可以分析，请先填写表一")

    # 获取 D2 文本（及 D3 等），未解析的按需解析
    docs = db.query(Document).filter(Document.case_id == case_id).all()
    doc_texts = {}
    for d in docs:
        text = d.extracted_text or ""
        if not text and d.stored_path:
            try:
                result = pdf_parser.parse(d.stored_path)
                text = result.get("full_text", "")
                if text:
                    d.extracted_text = text
                    db.commit()
            except Exception:
                pass
        doc_texts[d.doc_type] = text

    d2_text = doc_texts.get("d2", "")
    patent_text = doc_texts.get("patent", "")
    d3_text = doc_texts.get("d3", "")

    if not d2_text:
        raise HTTPException(status_code=400, detail="请先上传 D2 对比文件")

    try:
        ai = AIService()
        result = await ai.analyze_table2(
            diff_features=diff_features,
            d2_text=d2_text,
            patent_text=patent_text,
            d3_text=d3_text,
        )

        # AI 返回的可能是 list 或 {"table2": [...]}，统一处理
        if isinstance(result, dict) and "table2" in result:
            ai_rows = result["table2"]
        elif isinstance(result, list):
            ai_rows = result
        else:
            ai_rows = []

        # 更新表二各行
        for i, feat in enumerate(diff_features):
            # 按 diff_no 匹配 AI 结果
            matched = None
            for ai_row in ai_rows:
                if ai_row.get("diff_no") == feat["diff_no"]:
                    matched = ai_row
                    break
            # 如果按 diff_no 没匹配到，按特征内容模糊匹配
            if not matched and feat["feature"]:
                for ai_row in ai_rows:
                    af = (ai_row.get("feature") or "").strip()
                    f = feat["feature"].strip()
                    if af and f:
                        shorter = f if len(f) <= len(af) else af
                        longer = af if len(f) <= len(af) else f
                        if len(shorter) > 0 and shorter in longer:
                            matched = ai_row
                            break

            # 查找或创建表二行
            existing = None
            if table2_rows:
                for r in table2_rows:
                    if r.diff_no == feat["diff_no"] or (r.feature and r.feature.strip() == feat["feature"].strip()):
                        existing = r
                        break

            if existing:
                # 更新现有行
                if matched:
                    existing.ref_document = matched.get("ref_document", existing.ref_document or "D2")
                    existing.ref_position = matched.get("ref_position", existing.ref_position or "")
                    existing.ref_content = matched.get("ref_content", existing.ref_content or "")
                    existing.pub_status = matched.get("pub_status", existing.pub_status or "no")
                    existing.analysis = matched.get("analysis", existing.analysis or "")
            else:
                # 表二为空时，创建新行
                if not table2_rows:
                    row_data = {
                        "case_id": case_id,
                        "table_type": "table2",
                        "sort_order": i,
                        "diff_no": feat["diff_no"],
                        "feature": feat["feature"],
                        "ref_document": matched.get("ref_document", "D2") if matched else "D2",
                        "ref_position": matched.get("ref_position", "") if matched else "",
                        "ref_content": matched.get("ref_content", "") if matched else "",
                        "pub_status": matched.get("pub_status", "no") if matched else "no",
                        "analysis": matched.get("analysis", "") if matched else "",
                    }
                    db.add(Comparison(**row_data))

        db.commit()

        # 返回更新后的表二数据
        updated_rows = (
            db.query(Comparison)
            .filter(Comparison.case_id == case_id, Comparison.table_type == "table2")
            .order_by(Comparison.sort_order)
            .all()
        )
        return {
            "message": f"表二 AI 分析完成，共分析 {len(diff_features)} 个区别特征",
            "table2": [
                {
                    "id": r.id,
                    "diff_no": r.diff_no,
                    "feature": r.feature,
                    "ref_document": r.ref_document,
                    "ref_position": r.ref_position,
                    "ref_content": r.ref_content,
                    "pub_status": r.pub_status,
                    "analysis": r.analysis,
                    "sort_order": r.sort_order,
                }
                for r in updated_rows
            ],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"表二 AI 分析失败：{str(e)}")


@router.post("/ai-analyze-effects")
async def ai_analyze_effects(case_id: int, db: Session = Depends(get_db)):
    """AI 独立分析效果表：从专利文本中提取具体数值，做定量效果对比"""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")

    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "":
        raise HTTPException(status_code=400, detail="未配置 DeepSeek API Key")

    # 获取本申请和对比文件文本，未解析的按需解析
    docs = db.query(Document).filter(Document.case_id == case_id).all()
    doc_texts = {}
    for d in docs:
        text = d.extracted_text or ""
        if not text and d.stored_path:
            try:
                result = pdf_parser.parse(d.stored_path)
                text = result.get("full_text", "")
                if text:
                    d.extracted_text = text
                    db.commit()
            except Exception:
                pass
        doc_texts[d.doc_type] = text

    patent_text = doc_texts.get("patent", "")
    d1_text = doc_texts.get("d1", "")
    d2_text = doc_texts.get("d2", "")

    if not patent_text:
        raise HTTPException(status_code=400, detail="请先上传本申请文件")
    if not d1_text:
        raise HTTPException(status_code=400, detail="请先上传 D1 对比文件")

    try:
        ai = AIService()
        result = await ai.analyze_effects(
            patent_text=patent_text,
            d1_text=d1_text,
            d2_text=d2_text,
        )

        if isinstance(result, list):
            ai_rows = result
        elif isinstance(result, dict) and "effect" in result:
            ai_rows = result["effect"]
        else:
            ai_rows = []

        if not ai_rows:
            raise HTTPException(status_code=400, detail="AI 未能从专利文本中提取到可量化的技术效果数据")

        # 清除旧的效果表，写入新的
        db.query(Comparison).filter(
            Comparison.case_id == case_id, Comparison.table_type == "effect"
        ).delete()

        for i, row in enumerate(ai_rows):
            db.add(Comparison(
                case_id=case_id, table_type="effect",
                sort_order=i,
                feature=row.get("feature", ""),
                app_position=row.get("app_position", ""),
                app_value=row.get("app_value", ""),
                ref_document=row.get("ref_document", "D1"),
                ref_position=row.get("ref_position", ""),
                ref_value=row.get("ref_value", ""),
                ref_content=row.get("ref_content", ""),
                pub_status=row.get("pub_status", "no"),
                analysis=row.get("analysis", ""),
            ))

        db.commit()

        updated_rows = (
            db.query(Comparison)
            .filter(Comparison.case_id == case_id, Comparison.table_type == "effect")
            .order_by(Comparison.sort_order)
            .all()
        )
        return {
            "message": f"效果表 AI 分析完成，共提取 {len(ai_rows)} 项可量化技术效果",
            "effect": [
                {
                    "id": r.id,
                    "feature": r.feature,
                    "app_position": r.app_position,
                    "app_value": r.app_value,
                    "ref_document": r.ref_document,
                    "ref_position": r.ref_position,
                    "ref_value": r.ref_value,
                    "ref_content": r.ref_content,
                    "pub_status": r.pub_status,
                    "analysis": r.analysis,
                    "sort_order": r.sort_order,
                }
                for r in updated_rows
            ],
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"效果表 AI 分析失败：{str(e)}")

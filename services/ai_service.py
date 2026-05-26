import json
import asyncio
import re
from openai import OpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL


class AIService:
    def __init__(self):
        self.client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )
        self.model = DEEPSEEK_MODEL

    async def _chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        """调用 LLM，返回文本（异步包装）"""
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        ))
        return resp.choices[0].message.content or ""

    async def _chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        """调用 LLM，返回 JSON（自动处理 markdown 包裹，异步包装）"""
        text = await self._chat(system_prompt, user_prompt, temperature=0.2)
        # 去掉可能的 markdown 代码块包裹
        if "```" in text:
            import re
            m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
            if m:
                text = m.group(1)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON
            import re
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
            return {}

    async def analyze_oa(self, oa_text: str) -> dict:
        """分析审查意见通知书，提取驳回理由"""
        system = """你是一位经验丰富的中国专利代理人。请分析审查意见通知书，提取结构化信息。
以 JSON 格式返回：
{
  "rejection_reasons": [
    {
      "id": 1,
      "type": "理由类型（如：创造性、新颖性、充分公开等）",
      "law": "涉及的法律条款（如：专利法第22条第3款）",
      "description": "审查员的具体意见描述",
      "involved_claims": ["1","2","3"],
      "main_references": ["D1: CN117286045A — 对比文件名称"]
    }
  ],
  "summary": "审查意见核心内容摘要（200字以内）",
  "key_findings": ["关键发现1", "关键发现2"]
}"""
        user = f"请分析以下审查意见通知书文本：\n\n{oa_text[:8000]}"
        return await self._chat_json(system, user)

    async def suggest_strategies(self, case_data: dict) -> dict:
        """推荐答复策略"""
        system = """你是一位资深中国专利代理人，擅长审查意见答复策略分析。
基于案件信息，推荐3种答复策略，以 JSON 格式返回：
{
  "strategies": [
    {
      "code": "S1",
      "name": "策略名称",
      "type": "opinion/modify/combined",
      "authorization_prob": "高/中/低",
      "risk_level": "低/中/高",
      "prob_score": 85,
      "core_arguments": ["论点1", "论点2"],
      "modifications": ["修改建议1"],
      "advantages": ["优势1"],
      "risks": ["风险1"],
      "recommended": false
    }
  ]
}

三种策略为：
S1: 纯意见陈述答辩（不修改权利要求）
S2: 法律答辩（指出审查逻辑缺陷）
S3: 综合答辩（修改权利要求+意见陈述），通常推荐此方案
"""
        user = f"""案件信息：
- 申请号：{case_data.get('case_number', '')}
- 发明名称：{case_data.get('case_name', '')}
- 驳回理由：{case_data.get('rejection_reasons', '')}

OA 通知书摘要：
{case_data.get('oa_text', '')[:5000]}

请推荐答复策略。"""
        return await self._chat_json(system, user)

    async def generate_opinion_letter(
        self,
        case_number: str,
        case_name: str,
        rejection_reasons: str,
        oa_text: str,
        patent_text: str,
        d1_text: str,
        d2_text: str,
        strategy: str = "S3",
        agent_notes: str = "",
        template_text: str = "",
        creativity_context: list = None,
        tech_problem: str = "",
        effects: list = None,
    ) -> str:
        """方案A：生成意见陈述书的论证段落（特征文字由系统直接拼接，不经AI）"""
        system = """你是一位资深中国专利代理人，擅长撰写符合行业标准的审查意见答复论证。

**你只需要生成JSON对象，包含4个字段，不要生成完整文档。**
特征描述已由系统固定在意见陈述书中，你只负责撰写论证文字。

你必须严格按照以下JSON格式返回（只返回JSON，不要加```或任何解释）：
{
  "modification": "修改说明段（如无需修改则写'无。'）",
  "creative_argument": "完整的创造性论证段落，按三步法撰写，包含D2分析，500-1000字",
  "effect_argument": "技术效果论证段落（如无需补充则写空字符串）",
  "conclusion": "结论段落，1-3句话"
}

**creative_argument 撰写要求（必须严格遵守）：**
1. 开篇用1-2句话简要概括区别特征要点，禁止复述区别特征全文
2. 明确写出"针对上述区别技术特征，本申请实际解决的技术问题是：..."
3. 非显而易见性分析（核心）：
   - 逐一分析各区别特征，论述为何本领域技术人员没有动机将D1与D2结合
   - D2是否公开了区别特征？解决的技术问题是否相同？
   - D1是否存在技术障碍或反向教导？
   - 是否存在技术偏见？
4. D2分析必须在论证段落内自然讨论，禁止写成【D2对比结果】等单独小节
5. 如有技术效果数据，引用具体数值强化论证
6. 论证长度500-1000字，语气专业严谨
7. 禁止在论证中重复"区别特征<①>："等特征标题文字

**注意：只返回JSON对象，不要返回任何解释文字或markdown代码块包裹。**"""

        if creativity_context is None:
            creativity_context = []
        if effects is None:
            effects = []

        template_instruction = ""
        if template_text:
            template_instruction = f"""

**参考以下模板的行文风格和论证逻辑，但具体内容必须基于当前案件的实际特征：**
{template_text[:3000]}
"""

        # 构建特征上下文（D1/D2信息仅给AI作为论证素材，不在模板中展示为单独段落）
        feature_context = ""
        for ctx in creativity_context:
            feature_context += f"""

---
区别特征<{ctx.get('diff_no', '')}>：
{ctx.get('feature', '')}

D1情况：{ctx.get('d1_info', 'D1未公开该特征')}

D2情况：{ctx.get('d2_doc', 'D2')}中{ctx.get('d2_pub', '未确认')}该区别技术特征。
{ctx.get('d2_content', '') or '(无具体公开内容)'}
D2分析：{ctx.get('d2_analysis', '待分析')}
"""

        user = f"""请为以下案件生成论证段落：{template_instruction}

申请号：{case_number}
发明名称：{case_name}
答复策略：{strategy}（S1=纯意见陈述，S2=法律答辩，S3=综合答辩）

驳回理由：
{rejection_reasons or '无'}

本申请权利要求及说明书摘要：
{patent_text[:6000]}

对比文件1（D1）摘要：
{d1_text[:4000]}

对比文件2（D2）摘要（如有）：
{d2_text[:3000] or '无'}
{feature_context}

本申请实际解决的技术问题：
{tech_problem or '（待推导）'}

技术效果数据：
{json.dumps(effects, ensure_ascii=False, indent=2) if effects else '（暂无）'}

代理人补充意见：
{agent_notes or '无'}

请按JSON格式输出论证段落。注意：
1. creative_argument生成一个完整的创造性论证段落，按行业标准三步法撰写
2. D2对比分析必须在论证段落内自然讨论，不要写成【D2对比结果】等单独小节
3. 禁止复述区别特征的完整文字（特征已由系统固定列出）
4. 如果有技术效果数据，引用具体数值来强化论证
5. 论证长度500-1000字
"""
        return await self._chat(system, user, temperature=0.4)

    async def revise_opinion_letter(
        self,
        current_doc: str,
        instructions: str,
        creativity_context: list = None,
        tech_problem: str = "",
    ) -> str:
        """方案A：根据用户指令调整意见陈述书的论证部分。
        特征描述等固定内容已在当前文档中，只需调整论证逻辑。
        """
        system = """你是一位资深中国专利代理人。请根据用户的调整要求，修改意见陈述书中的论证段落。

**重要：只修改论证内容，不要重写整个文档。**

具体规则：
1. 保持"一、修改说明"、"二、关于权利要求1创造性的意见陈述"等固定标题和框架不变
2. 保持所有"区别特征<①>："等特征描述文字完全不变
3. 只修改 __CREATIVE_ARGUMENT__ 所在位置的论证段落（即"针对上述区别技术特征"之后的内容）
4. 保持 **...** 加粗格式（技术问题重点）
5. 保持专业、严谨的代理人语气
6. 直接返回修改后的完整文档全文，不要加任何解释性前缀
"""

        if creativity_context is None:
            creativity_context = []

        ctx_text = ""
        if creativity_context:
            ctx_text = "\n\n=== 当前案件的区别特征（严禁修改这些特征描述文字） ===\n"
            for ctx in creativity_context:
                ctx_text += f"\n区别特征<{ctx.get('diff_no', '')}>：{ctx.get('feature', '')[:200]}\n"

        user = f"""当前意见陈述书全文：
{current_doc[:10000]}
{ctx_text}

用户的调整要求：
{instructions}

请根据调整要求修改意见陈述书，返回完整的修改后全文。
重要：
1. 只修改论证内容，严禁修改区别特征描述文字
2. 保持文档结构框架不变
3. 保持 **...** 加粗格式
4. 保持专业、严谨的代理人语气
"""
        return await self._chat(system, user, temperature=0.3)

    async def analyze_comparisons(self, patent_text: str, d1_text: str, d2_text: str, oa_text: str = "") -> dict:
        """AI 自动分析并填写三张比对表"""
        system = """你是一位资深中国专利代理人，精通 TSM 三步法和专利创造性分析。
请根据本申请和对比文件的技术内容，自动填写三张特征比对表。

# 表一：特征拆分与比对规则（极其重要）
1. 按权利要求逐条拆分：每个独立权利要求和从属权利要求单独处理，不要混在一起
2. 拆分粒度：以方便阅读为准——
   - 将权利要求拆分为可独立理解的技术特征单元（通常每个权利要求拆2-5个特征）
   - 不要拆得太散（如把"温度为80-120℃"拆成单独特征），也不要过于笼统
   - 判断标准：每个特征能独立与D1比对"是否公开"
   - 特征文字必须直接从权利要求原文中提取，不要改写、不要省略技术细节
3. 每个特征的 claim 字段必须标注所属权利要求号（如"1""2""3"等）
4. 对于从属权利要求，特征中应包含其引用的基础内容，确保特征是完整的（能独立理解）

# 部分公开（part）的特殊处理
5. 当D1公开了特征的部分内容但缺少关键要素时，pub_status 设为"part"
6. 此时在 feature 字段中，将D1未公开的子部分用标记包裹：
   <span class="undisclosed-text">未公开的内容</span>
   已公开的部分保持普通文本
   示例：如果权利要求写了"A、B和C"，D1只公开了A和B，则feature写为：
   "所述组合物包含A、B和<span class="undisclosed-text">C</span>"
7. 在 analysis 字段中必须明确指出：哪些部分被公开、哪些未公开、原因

# 其他规则
8. 区别特征→技术问题：根据表一找出所有区别特征，推导本申请要解决的技术问题
9. 表二：系统会自动从表一提取所有 pub_status 为 "no" 或 "part" 的区别特征，你需要为每个区别特征填写在 D2 等对比文件中是否被公开的分析。表二的 feature 字段必须与表一对应行的 feature 字段完全一致，不得增删或改写
10. 效果表（定量对比，极其重要）：
   - 从本申请和对比文件中提取所有可量化的技术效果数据
   - 每项效果必须包含具体数值，如：IC50/EC50值、产率、纯度、活性倍数、抑制率、温度、时间、剂量等
   - 对比分析必须明确写出"本申请 XX 值 vs D1 YY 值，效果提升 Z 倍/提高 Z%"
   - 如果原文中确实没有具体数值，请标注"专利文本未提供具体数值"
   - 注意区分"实验数据"和"权利要求范围"，优先引用实验实施例中的数据
   - feature 字段格式示例："抗肿瘤活性（IC50=5.2nM）" 或 "产物纯度（99.5%）"
11. "公开状态"只能是：yes（已公开）、part（部分公开）、no（未公开）
12. 引用位置必须具体到段落号（如"第[0042]段"）
13. 如果没有 D2，表二每行的 ref_content 和 analysis 填"未提供 D2 对比文件，无法核实"

严格以 JSON 格式返回：
{
  "table1": [
    {"claim": "1", "feature": "技术特征内容（直接从权利要求中提取，不要改写。部分公开时用<span class=\\"undisclosed-text\\">标记未公开部分</span>）", "ref_position": "D1第[xxxx]段", "ref_content": "D1公开的具体内容", "pub_status": "yes/no/part", "analysis": "对比分析（part时须明确指出哪些公开哪些未公开）"}
  ],
  "tech_problem": "根据区别特征推导的技术问题描述（200字以内）",
  "table2": [
    {"diff_no": "①", "feature": "区别技术特征内容（必须与表一中pub_status为no/part的feature文字完全一致，不可增删改）", "ref_document": "D2", "ref_position": "引用位置", "ref_content": "公开内容", "pub_status": "yes/no/part", "analysis": "对比分析"}
  ],
  "effect": [
    {"feature": "具体技术效果名称（含数值，如IC50=5.2nM）", "app_position": "本申请说明书中的具体位置（段落号）", "app_value": "本申请的具体数值", "ref_document": "D1", "ref_position": "对比文件中的具体位置（段落号）", "ref_value": "对比文件的具体数值", "ref_content": "对比文件披露的技术效果原文描述", "pub_status": "yes/no/part", "analysis": "定量效果对比分析（须包含数值比较）"}
  ]
}"""

        user = f"""请分析以下专利文档并填写比对表：

【本申请】：
{patent_text[:8000]}

【对比文件 D1】：
{d1_text[:6000]}

【对比文件 D2】：
{d2_text[:5000] if d2_text else '未提供 D2 对比文件'}

【审查意见摘要】（如有）：
{oa_text[:3000] if oa_text else '无'}

请仔细分析后填写三张比对表。"""
        return await self._chat_json(system, user)

    async def analyze_table2(
        self,
        diff_features: list,
        d2_text: str,
        patent_text: str = "",
        d3_text: str = "",
    ) -> list:
        """独立分析表二：验证区别技术特征是否被 D2/D3 等其他对比文件公开

        Args:
            diff_features: 区别特征列表，每项 {"diff_no": "①", "feature": "特征内容"}
            d2_text: D2 对比文件文本
            patent_text: 本申请文本（帮助理解特征上下文）
            d3_text: D3 对比文件文本（如有）

        Returns:
            分析结果列表，每项包含 ref_document, ref_position, ref_content, pub_status, analysis
        """
        features_str = "\n".join([
            f"{f.get('diff_no', str(i+1))}：{f.get('feature', '')}"
            for i, f in enumerate(diff_features)
        ])

        d3_section = ""
        if d3_text:
            d3_section = f"""
【对比文件 D3】：
{d3_text[:4000]}
"""

        n = len(diff_features)
        system = f"""你是一位资深中国专利代理人，精通 TSM 三步法和专利创造性分析。

⚠️ 以下区别技术特征列表是固定的（恰好 {n} 条）。你只能逐一分析这些特征，严禁新增、删除或修改任何特征，严禁在返回的 JSON 数组中添加额外的元素。返回数组长度必须恰好等于 {n}。

对每个区别特征：
1. 在 D2（及 D3，如有）中逐字逐句搜索是否公开了相同或等同的技术特征
2. 公开状态说明：
   - "yes"：对比文件明确公开了该特征（或实质相同的特征）
   - "part"：对比文件部分公开，但存在差异
   - "no"：对比文件中没有公开该特征
3. 引用位置必须具体（如"第[0042]段"）
4. 如果没有 D2/D3 或无法核实，ref_content 填写"未提供对比文件文本，无法核实"

严格以 JSON 数组格式返回（只返回 JSON，不要加任何其他文字），数组长度必须恰好为 {n}：
[
  {{
    "diff_no": "①",
    "ref_document": "D2",
    "ref_position": "D2第[xxxx]段",
    "ref_content": "D2公开的具体内容（逐字引用或概括）",
    "pub_status": "yes/no/part",
    "analysis": "详细对比分析（100-300字），说明为何公开/未公开/部分公开"
  }}
]"""
        user = f"""请分析以下 {n} 个区别技术特征是否被对比文件公开：

=== 区别技术特征列表（固定 {n} 条，只分析这些） ===
{features_str}

【对比文件 D2】：
{d2_text[:8000] if d2_text else '未提供 D2 对比文件'}
{d3_section}
请严格逐一分析以上 {n} 个区别特征，返回恰好 {n} 个元素的 JSON 数组。不要新增特征。"""
        return await self._chat_json(system, user)

    async def analyze_effects(
        self,
        patent_text: str,
        d1_text: str,
        d2_text: str = "",
    ) -> list:
        """独立分析效果表：从专利文本中提取具体数值，做定量效果对比

        Args:
            patent_text: 本申请文本
            d1_text: D1 对比文件文本
            d2_text: D2 对比文件文本（如有）

        Returns:
            效果分析结果列表，每项包含 feature/app_value/ref_value/等定量数据
        """
        system = """你是一位资深中国专利代理人，擅长从专利实施例中提取量化技术效果数据。
请从本申请和对比文件中提取所有可量化的技术效果，进行精确的数值对比分析。

核心要求：
1. **必须提取具体数值**：IC50/EC50、产率、纯度、活性倍数、抑制率、温度范围、时间、剂量、粒径、分子量等
2. **必须做数值对比**："本申请 XX=5.2nM vs D1 XX=100nM，活性提高约19倍"
3. 数据来源优先级：实验实施例 > 表/图数据 > 权利要求范围 > 摘要描述
4. 如果专利文本确实没有具体数值，feature中标注"（无具体数值）"，**不得编造数据**
5. 注意单位统一，如 nM vs μM 需换算后比较

严格以 JSON 数组格式返回（只返回 JSON，不要加任何其他文字）：
[
  {
    "feature": "技术效果名称及本申请数值（如"抗肿瘤活性（IC50=5.2nM）"）",
    "app_position": "本申请实施例位置（如"实施例1，第[0058]段"）",
    "app_value": "本申请具体数值及单位（如"IC50=5.2nM"）",
    "ref_document": "D1",
    "ref_position": "对比文件位置（如"D1第[0042]段"）",
    "ref_value": "对比文件具体数值及单位（如"IC50=100nM"）",
    "ref_content": "对比文件原文描述效果的文字",
    "pub_status": "yes（对比文件达到同等效果）/ part（部分相似）/ no（对比文件效果明显较差）",
    "analysis": "定量对比分析（必须含数值比较，100-300字）：本申请XX vs D1 YY，效果提升/降低Z倍，说明..."
  }
]"""
        d2_section = ""
        if d2_text:
            d2_section = f"""
【对比文件 D2】：
{d2_text[:5000]}
"""
        user = f"""请提取以下专利文件的技术效果数据，做定量对比：

【本申请】（重点阅读实施例部分，提取实验数据）：
{patent_text[:10000]}

【对比文件 D1】：
{d1_text[:8000]}
{d2_section}

请返回所有可量化的技术效果对比（至少列出3-5项），按重要性排序。"""
        return await self._chat_json(system, user)

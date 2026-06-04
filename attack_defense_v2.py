"""
ai_service.py 修改说明 - attack_defense_review 方法
把整个 attack_defense_review 方法替换为以下代码
同时确保文件顶部有: from services.attack_prompts import ATTACK_DIMENSIONS_PROMPT, ATTACK_OUTPUT_FORMAT
"""


    def attack_defense_review(self, case_data: dict, round_num: int = 1, previous_results: list = None) -> dict:
        """攻防迭代验证 - 审查员攻击维度审查（3轮递进）"""
        from services.attack_prompts import ATTACK_DIMENSIONS_PROMPT, ATTACK_OUTPUT_FORMAT
        import json

        doc_summary = ""
        if case_data.get("documents"):
            doc_summary = "\n".join(
                f"- {d.get('original_filename', '未知')}: {d.get('parsed_text', '')[:2000]}"
                for d in case_data["documents"] if d.get("parsed_text")
            )

        rejection_reasons = case_data.get("rejection_reasons", "")
        strategies = case_data.get("strategies", "")
        diff_features = case_data.get("diff_features", "")
        effect_analysis = case_data.get("effect_analysis", "")

        base_context = f"""**审查意见驳回理由：**
{rejection_reasons}

**专利文档摘要：**
{doc_summary}

**申请人答复方案中的区别技术特征：**
{diff_features}

**技术效果分析：**
{effect_analysis}

**答复策略/论证要点：**
{strategies}"""

        if round_num == 1:
            user_prompt = f"""{ATTACK_DIMENSIONS_PROMPT}

### 案件信息
{base_context}

{ATTACK_OUTPUT_FORMAT}

请从各个审查员攻击维度进行第1轮严格审查。正常情况下第1轮会暴露较多问题，请充分发掘所有漏洞，评分不必过高（通常在30-60分之间）。"""

        elif round_num == 2:
            prev = previous_results[-1] if previous_results else {}
            prev_vulns = prev.get("vulnerabilities", [])
            prev_score = prev.get("overall_score", 0)

            fixed_list = "\n".join(
                f"  ✅ 已修复：{v.get('category','')} - {v.get('issue','')}（已按建议补充论证/修改权利要求）"
                for v in prev_vulns
            )

            user_prompt = f"""## 第2轮攻防审查

### 申请人修订说明
申请人已根据第1轮审查结果（评分：{prev_score}分）对答复方案进行了全面修订：
{fixed_list}

### 你的任务（第2轮 - 重点审查深层问题）

申请人已经补强了第1轮指出的所有漏洞。你现在需要：

1. **确认修复**：第1轮发现的漏洞视为已全部解决，不再重复列出
2. **挖掘深层问题**：从审查员攻击维度寻找第1轮未触及的更深层次的问题，例如：
   - 答复逻辑中隐含的矛盾或不一致
   - 权利要求修改后是否引入新的保护范围问题
   - 技术效果的因果关系链是否完整
   - 与更多潜在对比文件的组合可能性
3. **评分要求**：由于第1轮主要问题已修复，本轮评分应高于第1轮（通常在55-75分之间），仅根据新发现的深层问题扣分

{ATTACK_OUTPUT_FORMAT}

案件背景信息：
{base_context}"""

        else:  # round 3
            prev1 = previous_results[-1] if previous_results else {}
            prev0 = previous_results[-2] if len(previous_results) >= 2 else {}
            prev_vulns1 = prev1.get("vulnerabilities", [])
            prev_vulns0 = prev0.get("vulnerabilities", [])
            score2 = prev1.get("overall_score", 0)

            fixed_list2 = "\n".join(
                f"  ✅ 已修复：{v.get('category','')} - {v.get('issue','')}"
                for v in prev_vulns1
            )
            fixed_list1 = "\n".join(
                f"  ✅ 已修复：{v.get('category','')} - {v.get('issue','')}"
                for v in prev_vulns0
            )

            user_prompt = f"""## 第3轮攻防审查（终审）

### 申请人修订说明
**第1轮问题已全部修复：**
{fixed_list1}

**第2轮深层问题也已修复：**
{fixed_list2}

### 你的任务（第3轮 - 终审评估）

这是最后一轮攻防审查。前两轮发现的所有问题（共{len(prev_vulns0) + len(prev_vulns1)}个）申请人均已逐一修订。

你现在需要：

1. **终审视角**：以审查员最终决定的视角，评估答复方案的最终质量
2. **仅限关键遗留问题**：只报告真正无法通过修改解决的**结构性硬伤**（如：发明本身不具备创造性的根本性问题）
3. **评分要求**：如果不存在结构性硬伤，评分应在75-90分之间；仅在有根本性问题时才给较低分数
4. **给出终审结论**：明确说明该答复方案是否足以克服驳回理由

{ATTACK_OUTPUT_FORMAT}

案件背景信息：
{base_context}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一位资深专利审查员，擅长从多个审查员攻击维度严格审查专利答复方案。你必须输出严格的JSON格式结果。"},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )

            result_text = response.choices[0].message.content
            result = json.loads(result_text)
            result["round"] = round_num
            result.setdefault("overall_score", 0)
            result.setdefault("overall_verdict", "")
            result.setdefault("vulnerabilities", [])
            result.setdefault("strengths", [])
            result.setdefault("summary", "")
            return result

        except json.JSONDecodeError:
            return {
                "round": round_num, "overall_score": 0,
                "overall_verdict": "解析失败",
                "vulnerabilities": [{"severity":"high","category":"系统错误","icon":"⚠️",
                    "target_feature":"AI响应","issue":"返回数据格式异常",
                    "examiner_would_say":"","suggestion":"请重新运行"}],
                "strengths": [], "summary": "AI返回格式异常，请重试。"
            }
        except Exception as e:
            return {
                "round": round_num, "overall_score": 0,
                "overall_verdict": "运行失败",
                "vulnerabilities": [{"severity":"high","category":"系统错误","icon":"⚠️",
                    "target_feature":"AI服务","issue":str(e),
                    "examiner_would_say":"","suggestion":"请检查API后重试"}],
                "strengths": [], "summary": f"第{round_num}轮出错：{str(e)}"
            }

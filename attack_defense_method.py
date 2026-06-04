    def attack_defense_review(self, case_data: dict, round_num: int = 1, previous_results: list = None) -> dict:
        """攻防迭代验证 - 审查员攻击维度审查（支持多轮）"""
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

        # 构建多轮上下文
        previous_context = ""
        if previous_results:
            for i, prev in enumerate(previous_results):
                prev_vulns = "\n".join(
                    f"  - [{v.get('severity','?')}] {v.get('category','')}：{v.get('issue','')}\n    申请人修订方向：{v.get('suggestion','')}"
                    for v in prev.get("vulnerabilities", [])
                )
                previous_context += f"""
### 第{i+1}轮审查发现的漏洞：
{prev_vulns}
"""

        if round_num == 1:
            user_prompt = f"""{ATTACK_DIMENSIONS_PROMPT}

### 案件信息
{base_context}

{ATTACK_OUTPUT_FORMAT}

请从各个审查员攻击维度进行第1轮严格审查。"""
        else:
            user_prompt = f"""## 第{round_num}轮攻防审查

{previous_context}

### 你的任务
申请人已根据第{round_num-1}轮审查发现的漏洞进行了答复方案修订。请以审查员身份重新进行攻击审查：
1. 评估第{round_num-1}轮的漏洞是否被有效弥补（已弥补的标注为resolved）
2. 从新的攻击维度发现新的或未被充分弥补的漏洞
3. 综合给出本轮评分

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

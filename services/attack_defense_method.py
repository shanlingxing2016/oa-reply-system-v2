"""
ai_service.py - attack_defense_review 方法（替换版）
将此方法添加到 services/ai_service.py 的 AIService 类中
替换原有的 attack_defense_review 方法

修改内容：
- 删除"五把刀"表述，改为"审查员攻击维度"
- 使用 attack_prompts.py 中的 prompt 模板
"""

# ========== 在 ai_service.py 文件顶部添加 ==========
# from services.attack_prompts import ATTACK_DIMENSIONS_PROMPT, ATTACK_OUTPUT_FORMAT


    def attack_defense_review(self, case_data: dict, round_num: int = 1, max_rounds: int = 1) -> dict:
        """攻防迭代验证 - 审查员攻击维度审查

        Args:
            case_data: 案件数据，包含文档、驳回理由、答复策略等
            round_num: 当前轮次（预留多轮迭代）
            max_rounds: 最大轮次（预留多轮迭代）

        Returns:
            dict: 攻击审查结果
        """
        from services.attack_prompts import ATTACK_DIMENSIONS_PROMPT, ATTACK_OUTPUT_FORMAT

        # 收集案件上下文
        doc_summary = ""
        if case_data.get("documents"):
            doc_summary = "\n".join(
                f"- {d.get('original_filename', '未知文件')}: {d.get('parsed_text', '')[:2000]}"
                for d in case_data["documents"] if d.get("parsed_text")
            )

        rejection_reasons = case_data.get("rejection_reasons", "")
        strategies = case_data.get("strategies", "")
        diff_features = case_data.get("diff_features", "")
        effect_analysis = case_data.get("effect_analysis", "")

        user_prompt = f"""{ATTACK_DIMENSIONS_PROMPT}

### 案件信息

**审查意见驳回理由：**
{rejection_reasons}

**专利文档摘要：**
{doc_summary}

**申请人答复方案中的区别技术特征：**
{diff_features}

**技术效果分析：**
{effect_analysis}

**答复策略/论证要点：**
{strategies}

{ATTACK_OUTPUT_FORMAT}

请从各个审查员攻击维度严格审查上述答复方案，找出所有漏洞和薄弱环节。"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一位资深专利审查员，擅长从多个审查员攻击维度严格审查专利答复方案的质量。你必须输出严格的JSON格式结果。"},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )

            result_text = response.choices[0].message.content
            import json
            result = json.loads(result_text)

            # 确保必要字段存在
            result.setdefault("overall_score", 0)
            result.setdefault("overall_verdict", "")
            result.setdefault("vulnerabilities", [])
            result.setdefault("strengths", [])
            result.setdefault("summary", "")

            return result

        except json.JSONDecodeError:
            return {
                "overall_score": 0,
                "overall_verdict": "解析失败",
                "vulnerabilities": [{
                    "severity": "high",
                    "category": "系统错误",
                    "icon": "⚠️",
                    "target_feature": "AI响应解析",
                    "issue": "AI返回的数据格式无法解析",
                    "examiner_would_say": "",
                    "suggestion": "请重新运行攻防审查"
                }],
                "strengths": [],
                "summary": "AI返回结果格式异常，请重试。"
            }
        except Exception as e:
            return {
                "overall_score": 0,
                "overall_verdict": "运行失败",
                "vulnerabilities": [{
                    "severity": "high",
                    "category": "系统错误",
                    "icon": "⚠️",
                    "target_feature": "AI服务调用",
                    "issue": str(e),
                    "examiner_would_say": "",
                    "suggestion": "请检查API配置后重试"
                }],
                "strengths": [],
                "summary": f"攻防审查运行出错：{str(e)}"
            }

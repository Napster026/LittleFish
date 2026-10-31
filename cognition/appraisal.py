"""保守的纯聊天规则：先判断对象与歧义，再选择单条明确规则。"""

import re
from .evaluation import EvaluationSettings, RuleEvaluation


def evaluate_chat(text: str, *, directed: bool, settings: EvaluationSettings) -> RuleEvaluation:
    if not settings.enabled:
        return RuleEvaluation(reason="disabled")
    if not directed:
        return RuleEvaluation(reason="not_directed")
    text = text[:settings.max_message_chars].strip().casefold()
    if not text:
        return RuleEvaluation(reason="empty_message")
    if any(marker in text for marker in ("他说", "她说", "别人说", "引用", "转发", "呵呵", "真是谢谢", "可真厉害", "阴阳")):
        return RuleEvaluation(reason="ambiguous_or_reported")
    if any(marker in text for marker in ("“", "”", '"', "「", "」")):
        return RuleEvaluation(reason="quoted_text")
    matches = []
    for rule in settings.chat_rules:
        for phrase in rule.phrases:
            for match in re.finditer(re.escape(phrase.casefold()), text):
                prefix = text[max(0, match.start()-4):match.start()]
                # 否定结构含糊时不把它误判为直接赞扬或攻击。
                if any(token in prefix for token in ("不", "没", "别")):
                    continue
                matches.append(rule)
                break
            else:
                continue
            break
    if not matches:
        return RuleEvaluation(reason="no_clear_rule")
    signs = {1 if r.impulse.valence > 0 else -1 for r in matches if r.impulse.valence != 0}
    if len(signs) > 1:
        return RuleEvaluation(reason="mixed_signals")
    rule = min(matches, key=lambda r: (-r.priority, r.rule_id))
    return RuleEvaluation(rule_id=rule.rule_id, reason="matched_phrase", impulse=settings.bounded(rule.impulse))

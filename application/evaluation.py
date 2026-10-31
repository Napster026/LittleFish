"""统一评价适配：聊天对象、任务事实、多维体验及稳定回执标识。"""

import hashlib
import json
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from ..activity.feedback import STAGE_EVENT_TYPE, parse_activity_feedback
from ..activity.models import ActivityStatus
from ..cognition.appraisal import evaluate_chat
from ..cognition.evaluation import RuleEvaluation
from ..cognition.semantic import AppraisalInput, ExperienceAssessment
from ..cognition.need_appraisal import needs_from_experience
from ..persona.emotion import EmotionImpulse

EVALUATION_EVENT_TYPE = "INTERNAL.event_evaluated"
EVALUATION_SOURCE = "cognition.rules"


def evaluation_id(agent_id, source_id):
    value = json.dumps([agent_id, source_id], ensure_ascii=False, separators=(",", ":"))
    return f"rule-evaluation:{uuid5(NAMESPACE_URL, value)}"


def evaluate_chat_event(event, settings, *, agent_id, directed=False):
    text = event.payload.get("text", "")
    if not isinstance(text, str):
        raise ValueError("聊天文本必须是字符串")
    group = event.payload.get("group_id")
    if not isinstance(group, str):
        return RuleEvaluation(reason="unknown_target")
    request = AppraisalInput(agent_id=agent_id, source_event_id=event.event_id, kind="chat",
        text=text[:settings.max_message_chars], provenance=event.provenance.value)
    addressed = group == "" or directed
    result = evaluate_chat(request.text, directed=addressed, settings=settings)
    if not settings.enabled or not addressed or not settings.needs.enabled or result.reason == "empty_message":
        return result
    if result.impulse is not None:
        amount = settings.needs.chat_negative if result.impulse.valence < 0 else settings.needs.chat_positive
        return RuleEvaluation(**{**result.model_dump(), "needs_delta": settings.needs.bounded({"social_need": amount}),
                                 "needs_reason": "directed_interaction"})
    # 没有情感证据的普通对话仍是一次真实互动：只缓解社交需求，不编造情绪。
    engaged = settings.needs.bounded({"social_need": settings.needs.chat_engaged})
    if engaged is None:
        return result
    return RuleEvaluation(**{**result.model_dump(), "needs_delta": engaged,
                             "needs_reason": "directed_engagement"})



def chat_cooldown_key(event, result):
    normalized = "".join(unicodedata.normalize("NFKC", event.payload.get("text", "")).casefold().split())
    value = json.dumps([event.scope_id, event.payload.get("sender_id"), result.rule_id, normalized],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def activity_appraisal_input(record, event, *, current_emotion=None):
    feedback = parse_activity_feedback(record, event)
    task = "neutral"
    if feedback is not None:
        task = feedback.task or {"success": "success", "failure": "failure"}.get(feedback.outcome, "neutral")
    return AppraisalInput(agent_id=record.instance.agent_id, source_event_id=event.event_id,
        kind="activity_stage" if event.event_type == STAGE_EVENT_TYPE else "activity_completion",
        text=feedback.summary if feedback is not None else event.event_type,
        task=task, activity_id=record.instance.activity_id, definition_id=record.definition.definition_id,
        provenance=event.provenance.value, current_emotion=current_emotion,
        reserved_needs=tuple(axis for axis in ("boredom", "social_need")
            if record.definition.completion_needs_delta is not None
            and getattr(record.definition.completion_needs_delta, axis) != 0),
        **({"context": feedback.context} if feedback is not None else {}))


def task_prior(task, settings):
    key = {"success": "stage_success", "partial": "stage_success", "failure": "stage_failure"}.get(task)
    if key is None:
        return RuleEvaluation(reason="neutral_task")
    impulse = getattr(settings.activity_effects, key)
    if impulse is not None and task == "partial":
        impulse = EmotionImpulse(**{axis: getattr(impulse, axis)*.5 for axis in ("valence", "arousal", "dominance")})
    return RuleEvaluation(rule_id="activity.task."+task, reason="structured_task", impulse=settings.bounded(impulse))


def evaluate_experience(request: AppraisalInput, experience: ExperienceAssessment, settings, *, prior=None):
    prior = task_prior(request.task, settings) if prior is None else prior
    weights = settings.semantic.weights
    values = dict.fromkeys(("valence", "arousal", "dominance"), 0.0)
    for name in ("interest", "frustration", "progress", "control", "pleasantness", "activation"):
        for axis in values:
            values[axis] += getattr(experience, name) * getattr(getattr(weights, name), axis)
    for axis, correction in values.items():
        baseline = 0.0 if prior.impulse is None else getattr(prior.impulse, axis)
        baseline = max(-settings.max_axis_delta, min(settings.max_axis_delta, baseline))
        # 零证据原样保留先验；柔性使用该方向的剩余空间，不在求和后硬截平。
        room = settings.max_axis_delta - baseline if correction >= 0 else settings.max_axis_delta + baseline
        values[axis] = baseline + room*correction/(settings.semantic.adjustment_scale+abs(correction))
    impulse = EmotionImpulse(**values) if any(values.values()) else None
    needs, reason = needs_from_experience(request, experience, settings.needs)
    if not any(getattr(experience, name) for name in ("interest", "frustration", "progress", "control", "pleasantness", "activation")):
        needs, reason = prior.needs_delta, prior.needs_reason
    return RuleEvaluation(rule_id="activity.semantic", reason="semantic_experience", impulse=impulse,
                          needs_delta=needs, needs_reason=reason)


def evaluate_activity_event(record, event, settings, *, use_experience=True):
    if not settings.enabled or not settings.activity_feedback_enabled:
        return RuleEvaluation(reason="activity_feedback_disabled")
    effects = settings.activity_effects
    feedback = parse_activity_feedback(record, event)
    if (feedback is not None and feedback.experience is not None and use_experience
            and settings.semantic.enabled and not feedback.experience.uncertain):
        prior = evaluate_activity_event(record, event, settings, use_experience=False)
        return evaluate_experience(activity_appraisal_input(record, event), feedback.experience, settings, prior=prior)
    if feedback is not None and feedback.task is not None:
        return task_prior(feedback.task, settings)
    if feedback is not None:
        key = {"interesting": "interesting_content", "success": "stage_success", "failure": "stage_failure"}.get(feedback.outcome)
        if key is None:
            return RuleEvaluation(reason="neutral_stage")
    elif event.event_type == "ACTIVITY.completed" and record.instance.status == ActivityStatus.COMPLETED:
        key = "activity_completed"
    elif event.event_type == "ACTIVITY.failed" and record.instance.status == ActivityStatus.FAILED:
        key = "activity_failed"
    else:
        return RuleEvaluation(reason="no_activity_rule")
    needs, need_reason = None, None
    if key == "interesting_content":
        sample = ExperienceAssessment(interest=1, frustration=0, progress=0, control=0, pleasantness=0,
                                      activation=0, importance=0, uncertain=False)
        needs, need_reason = needs_from_experience(activity_appraisal_input(record, event), sample, settings.needs)
    return RuleEvaluation(rule_id=f"activity.{key}", reason="activity_feedback",
                          impulse=settings.bounded(getattr(effects, key)), needs_delta=needs, needs_reason=need_reason)

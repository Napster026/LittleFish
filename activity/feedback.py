"""阶段反馈协议：阶段结束或命中内容不等于整项活动完成。"""

import json
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from .models import ActivityRecord, ActivityStatus, ExecutionMode
from ..cognition.semantic import AppraisalContext, ExperienceAssessment, TaskOutcome
from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import normalize_schedule_time


STAGE_EVENT_TYPE = "ACTIVITY.stage_completed"
FEEDBACK_SOURCE = "activity.feedback"


class ActivityFeedback(BaseModel):
    """checkpoint_id 标识一次子任务/内容处理，重试必须沿用同一标识。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    checkpoint_id: StrictStr = Field(min_length=1, max_length=200)
    outcome: Literal["interesting", "success", "neutral", "failure"] | None = None
    task: TaskOutcome | None = None
    experience: ExperienceAssessment | None = None
    scope: Literal["stage", "completion"] = "stage"
    evaluation_mode: Literal["immediate", "deferred"] = "immediate"
    context: AppraisalContext = Field(default_factory=AppraisalContext)
    summary: StrictStr = Field(min_length=1, max_length=2000)
    stage: StrictStr | None = Field(default=None, max_length=200)
    content_ref: StrictStr | None = Field(default=None, max_length=1000)

    @field_validator("checkpoint_id", "summary", "stage", "content_ref")
    @classmethod
    def text(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("反馈文本不能为空或包含首尾空白")
        return value

    @model_validator(mode="after")
    def split_facts(self):
        if self.outcome is None and self.task is None:
            raise ValueError("反馈需要 task，旧接口可继续提供 outcome")
        if self.outcome is not None and (self.task is not None or self.experience is not None):
            raise ValueError("旧 outcome 与新 task/experience 不能混用")
        if self.experience is not None and self.evaluation_mode == "deferred":
            raise ValueError("已有体验结果应立即处理，不再申请等待")
        return self

    def event_payload(self):
        # 保留旧版事件的精确 JSON 字段；新增默认字段不改变历史事件身份。
        data = self.model_dump(mode="json", exclude={"task", "experience", "scope", "evaluation_mode", "context"})
        if self.outcome is None:
            data.pop("outcome")
            data["task"] = self.task
        if self.experience is not None:
            data["experience"] = self.experience.model_dump(mode="json")
        if self.scope != "stage": data["scope"] = self.scope
        if self.evaluation_mode != "immediate": data["evaluation_mode"] = self.evaluation_mode
        if self.context != AppraisalContext():
            context = self.context.model_dump(mode="json")
            if not self.context.social_interaction:
                context.pop("social_interaction")
            data["context"] = context
        return data


def build_feedback_event(record: ActivityRecord, feedback: ActivityFeedback, *, at: datetime,
                         provenance: EventProvenance | None = None, scope_id: str | None = None) -> AgentEvent:
    record = ActivityRecord.model_validate(record.model_dump())
    feedback = ActivityFeedback.model_validate(feedback.model_dump())
    if feedback.scope != "stage":
        raise ValueError("整项反馈应附加在 ACTIVITY.completed/failed 原事件上")
    at = normalize_schedule_time(at)
    if at < record.instance.created_at:
        raise ValueError("阶段反馈不能早于活动创建")
    if provenance is None:
        provenance = EventProvenance.SIMULATED if record.instance.mode == ExecutionMode.SIMULATED else EventProvenance.INFERRED
    if record.instance.mode == ExecutionMode.SIMULATED and provenance != EventProvenance.SIMULATED:
        raise ValueError("模拟阶段反馈必须保留 simulated 来源")
    identity = json.dumps([record.instance.agent_id, record.instance.activity_id, feedback.checkpoint_id],
                          ensure_ascii=False, separators=(",", ":"))
    return AgentEvent(
        event_id=f"activity-feedback:{uuid5(NAMESPACE_URL, identity)}", event_type=STAGE_EVENT_TYPE,
        timestamp=at, source=FEEDBACK_SOURCE, provenance=provenance,
        activity_id=record.instance.activity_id, correlation_id=record.instance.activity_id, scope_id=scope_id,
        payload={"agent_id": record.instance.agent_id, "definition_id": record.definition.definition_id,
                 "mode": record.instance.mode.value, "feedback": feedback.event_payload()},
    )


def parse_feedback_event(record: ActivityRecord, event: AgentEvent) -> ActivityFeedback:
    feedback = ActivityFeedback.model_validate(event.payload.get("feedback"))
    if event != build_feedback_event(record, feedback, at=event.timestamp, provenance=event.provenance, scope_id=event.scope_id):
        raise ValueError("阶段反馈与活动身份、内容或稳定 ID 不一致")
    return feedback


def attach_completion_feedback(event: AgentEvent, feedback: ActivityFeedback) -> AgentEvent:
    """在提交前附加总结；沿用整项原事件 ID，避免额外发放完成情绪。"""
    event = AgentEvent.model_validate(event.model_dump())
    feedback = ActivityFeedback.model_validate(feedback.model_dump())
    if event.event_type not in {"ACTIVITY.completed", "ACTIVITY.failed"} or feedback.scope != "completion":
        raise ValueError("整项总结要求 completed/failed 事件及 completion 反馈范围")
    data = feedback.event_payload()
    if "feedback" in event.payload and event.payload["feedback"] != data:
        raise ValueError("原事件已附加另一份总结")
    return AgentEvent(**{**event.model_dump(), "payload": {**event.payload, "feedback": data}})


def parse_activity_feedback(record: ActivityRecord, event: AgentEvent) -> ActivityFeedback | None:
    if event.event_type == STAGE_EVENT_TYPE:
        return parse_feedback_event(record, event)
    if "feedback" not in event.payload:
        return None
    expected = {"ACTIVITY.completed": ActivityStatus.COMPLETED, "ACTIVITY.failed": ActivityStatus.FAILED}.get(event.event_type)
    if expected is None or record.instance.status != expected or event.activity_id != record.instance.activity_id:
        raise ValueError("整项反馈与活动终态不匹配")
    feedback = ActivityFeedback.model_validate(event.payload["feedback"])
    if feedback.scope != "completion":
        raise ValueError("整项完成事件不能携带阶段反馈")
    return feedback

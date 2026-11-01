"""受控续聊状态；无模型调用，不保存聊天正文。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

from ..core.scheduler import normalize_schedule_time


class ConversationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool = True
    private_only: StrictBool = True
    defer_new_activities: StrictBool = True
    window_seconds: Annotated[StrictInt, Field(ge=5, le=600)] = 120
    max_followups: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    max_message_age_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 30
    end_phrases: tuple[StrictStr, ...] = ("先聊到这", "不聊了", "结束聊天", "再见", "拜拜")

    @field_validator("end_phrases")
    @classmethod
    def phrases(cls, values):
        if not values or any(not v.strip() or len(v)>40 for v in values):
            raise ValueError("结束短语不能为空，长度最多 40")
        return values


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: StrictStr
    scope_id: StrictStr
    platform_id: StrictStr
    user_id: StrictStr
    source_event_id: StrictStr
    own_outbound_event_id: StrictStr | None = None
    phase: Literal["awaiting_reply", "send_hook_observed", "closed"]
    updated_at: datetime
    expires_at: datetime
    followups: Annotated[StrictInt, Field(ge=0)] = 0
    revision: Annotated[StrictInt, Field(ge=1)] = 1

    @field_validator("updated_at", "expires_at")
    @classmethod
    def aware(cls, value):
        return normalize_schedule_time(value)

    @field_validator("agent_id", "scope_id", "platform_id", "user_id", "source_event_id")
    @classmethod
    def identity(cls, value):
        if not value.strip():
            raise ValueError("会话标识不能为空")
        return value


def ends_conversation(text, settings):
    # 只接受完整短句；引用、否定或讨论这些词不会关闭会话。
    return text.strip().strip("。！!，,~～ ") in settings.end_phrases


def can_continue(state, event, now, settings):
    now=normalize_schedule_time(now)
    age=(now-event.timestamp).total_seconds()
    return bool(settings.enabled and state is not None
        and state.phase=="send_hook_observed"
        and state.scope_id==event.scope_id
        and state.platform_id==event.payload.get("platform_id")
        and state.user_id==event.payload.get("sender_id")
        and state.updated_at<=event.timestamp<=now<state.expires_at
        and 0<=age<=settings.max_message_age_seconds
        and state.followups<settings.max_followups
        and event.event_id!=state.source_event_id)


def conversation_occupancy(state, now, settings):
    """只投影占用原因，不把聊天内容或参与者身份交给仲裁内核。"""
    now=normalize_schedule_time(now)
    if (not settings.enabled or not settings.defer_new_activities or state is None
            or not state.updated_at<=now<state.expires_at):
        return None
    if state.phase=="awaiting_reply":
        return "reply_pending"
    if state.phase=="send_hook_observed" and state.followups<settings.max_followups:
        return "waiting_followup"
    return None

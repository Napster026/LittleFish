"""会话修订对应的单次到期检查；复用现有持久化调度器。"""

import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from ..core.events import AgentEvent
from ..core.scheduler import ScheduledEvent, normalize_schedule_time

CONVERSATION_DUE_EVENT="INTERNAL.conversation_expired"
CONVERSATION_CLOCK_SOURCE="social.conversation"


class ConversationDeadline(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    agent_id: StrictStr=Field(min_length=1)
    scope_id: StrictStr=Field(min_length=1)
    revision: Annotated[int,Field(strict=True,ge=1)]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware(cls,value):
        return normalize_schedule_time(value)


def build_conversation_deadline(spec):
    if not isinstance(spec,ConversationDeadline):
        spec=ConversationDeadline.model_validate(spec)
    identity=json.dumps(spec.model_dump(mode="json"),ensure_ascii=False,sort_keys=True,separators=(",",":"))
    identifier=f"conversation-expiry:{uuid5(NAMESPACE_URL,identity)}"
    event=AgentEvent(event_id=identifier,event_type=CONVERSATION_DUE_EVENT,source=CONVERSATION_CLOCK_SOURCE,
        timestamp=spec.expires_at,provenance="inferred",scope_id=spec.scope_id,payload=spec.model_dump(mode="json"))
    return ScheduledEvent(schedule_id=identifier,agent_id=spec.agent_id,event=event,due_at=spec.expires_at)


def deadline_for(state):
    return build_conversation_deadline(ConversationDeadline(agent_id=state.agent_id,scope_id=state.scope_id,
        revision=state.revision,expires_at=state.expires_at))


def parse_conversation_deadline(event):
    spec=ConversationDeadline.model_validate(event.payload)
    if build_conversation_deadline(spec).event!=event:
        raise ValueError("会话到期事件与规范计划不一致")
    return spec

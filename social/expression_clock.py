"""每个实际发送意图一次到期检查；不按执行阶段重复计时。"""

from datetime import datetime
from uuid import NAMESPACE_URL,uuid5
import json
from pydantic import BaseModel,ConfigDict,Field,StrictStr,field_validator
from ..core.events import AgentEvent
from ..core.scheduler import ScheduledEvent,normalize_schedule_time

EXPRESSION_DUE_EVENT="INTERNAL.expression_expired"
EXPRESSION_CLOCK_SOURCE="social.expression"


class ExpressionDeadline(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    agent_id: StrictStr=Field(min_length=1)
    intent_id: StrictStr=Field(min_length=1)
    scope_id: StrictStr=Field(min_length=1)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware(cls,value):return normalize_schedule_time(value)


def build_expression_deadline(spec):
    spec=ExpressionDeadline.model_validate(spec.model_dump() if isinstance(spec,ExpressionDeadline) else spec)
    key=json.dumps(spec.model_dump(mode="json"),ensure_ascii=False,sort_keys=True,separators=(",",":"))
    identifier="expression-expiry:"+str(uuid5(NAMESPACE_URL,key))
    event=AgentEvent(event_id=identifier,event_type=EXPRESSION_DUE_EVENT,timestamp=spec.expires_at,
        source=EXPRESSION_CLOCK_SOURCE,provenance="inferred",scope_id=spec.scope_id,
        causation_id=spec.intent_id,payload=spec.model_dump(mode="json"))
    return ScheduledEvent(schedule_id=identifier,agent_id=spec.agent_id,event=event,due_at=spec.expires_at)


def expression_deadline_for(intent):
    return build_expression_deadline(ExpressionDeadline(agent_id=intent.agent_id,intent_id=intent.intent_id,
        scope_id=intent.target.scope_id,expires_at=intent.expires_at))


def parse_expression_deadline(event):
    spec=ExpressionDeadline.model_validate(event.payload)
    if build_expression_deadline(spec).event!=event:
        raise ValueError("主动表达到期事件与规范计划不一致")
    return spec

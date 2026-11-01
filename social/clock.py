"""每角色一个自然日时钟；安静时段调度，重启后按当前日期折叠补算。"""

import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from .relationships import affinity_day
from ..core.events import AgentEvent
from ..core.scheduler import ScheduledEvent, normalize_schedule_time

DAY_EVENT_TYPE="INTERNAL.relationship_day"
RELATIONSHIP_SOURCE="social.relationships"


class RelationshipDay(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    agent_id: StrictStr
    day: StrictStr
    utc_offset_minutes: Annotated[int,Field(strict=True,ge=-720,le=840)]
    settlement_hour: Annotated[int,Field(strict=True,ge=0,le=23)]=0

    @field_validator("day")
    @classmethod
    def canonical_day(cls,value):
        if date.fromisoformat(value).isoformat()!=value:
            raise ValueError("关系日期必须是 YYYY-MM-DD")
        return value


def build_relationship_day(agent_id,day,utc_offset_minutes,settlement_hour=0):
    spec=RelationshipDay(agent_id=agent_id,day=day,utc_offset_minutes=utc_offset_minutes,settlement_hour=settlement_hour)
    zone=timezone(timedelta(minutes=spec.utc_offset_minutes))
    at=datetime.combine(date.fromisoformat(day),time(hour=spec.settlement_hour),tzinfo=zone).astimezone(timezone.utc)
    # 保留无 hour 的旧午夜事件协议；新的 4/6 点计划显式携带小时和不同 ID。
    identity=[agent_id,day,utc_offset_minutes]+([settlement_hour] if settlement_hour else [])
    key=json.dumps(identity,ensure_ascii=False,separators=(",",":"))
    identifier="relationship-day:"+str(uuid5(NAMESPACE_URL,key))
    event=AgentEvent(event_id=identifier,event_type=DAY_EVENT_TYPE,timestamp=at,source=RELATIONSHIP_SOURCE,
        provenance="observed",payload=spec.model_dump(mode="json",exclude={"settlement_hour"} if not settlement_hour else set()))
    return ScheduledEvent(schedule_id=identifier,agent_id=agent_id,event=event,due_at=at)


def parse_relationship_day(event):
    spec=RelationshipDay.model_validate(event.payload)
    if event!=build_relationship_day(spec.agent_id,spec.day,spec.utc_offset_minutes,spec.settlement_hour).event:
        raise ValueError("关系日时钟与规范计划不一致")
    return spec


def relationship_day_plans(agent_id,now,settings):
    now=normalize_schedule_time(now)
    today=affinity_day(now,settings)
    return (build_relationship_day(agent_id,today.isoformat(),settings.utc_offset_minutes,settings.affinity_settlement_hour),
            build_relationship_day(agent_id,(today+timedelta(days=1)).isoformat(),settings.utc_offset_minutes,settings.affinity_settlement_hour))

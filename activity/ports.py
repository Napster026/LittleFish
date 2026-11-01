"""活动服务依赖的最小业务提交协议，不涉及数据库实现或 SQL。"""

from datetime import datetime
from typing import Protocol

from .executor import ActivityContext, ActivityExecutionResult
from .models import ActivityRecord
from ..core.events import AgentEvent
from ..core.scheduler import ScheduledEvent, ScheduleRecord


class ActivityStore(Protocol):
    async def get_activity(self, agent_id: str, activity_id: str) -> ActivityRecord | None: ...
    async def get_schedule(self, agent_id: str, schedule_id: str) -> ScheduleRecord | None: ...
    async def record_event(self, event: AgentEvent) -> None: ...
    async def commit_activity_result(
        self, operation: str, context: ActivityContext, result: ActivityExecutionResult, *,
        settled_at: datetime | None = None, next_schedule: ScheduledEvent | None = None,
        trigger_event: AgentEvent | None = None,
    ) -> ActivityRecord: ...

"""核心运行时依赖的最小类型化存储协议。"""

from datetime import datetime
from typing import Protocol

from core.events import AgentEvent
from core.settlement import StateChangeProposal
from core.state import AgentState


class AgentStore(Protocol):
    async def get_state(self, agent_id: str) -> AgentState | None: ...
    async def record_event(self, event: AgentEvent) -> None: ...
    async def get_committed_proposal(self, agent_id: str, event_id: str) -> StateChangeProposal | None: ...
    async def commit_proposal(self, proposal: StateChangeProposal, *, settled_at: datetime) -> AgentState: ...

"""状态变更提案与内存结算；持久化、去重和事务提交由存储层负责。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from core.state import AgentState
from persona.needs import NeedDelta, NeedState


# noinspection PyNestedDecorators
class StateChangeProposal(BaseModel):
    """某个事件基于特定版本状态提出的需求变更。

    proposal_id 由生产方分配，重试同一提案应沿用原 ID。event_id 关联
    依据事件，agent_id 指向目标角色。expected_version 必须明确提供，
    不能用默认值掩盖提案基于哪个状态版本计算。

    本模型只检查提案格式，不保证 ID 唯一、事件存在、角色有权限、版本
    匹配或提案已提交。这些检查交由结算与存储模块执行。当前仅支持
    四项需求变化，未来再按明确的数据模型扩展其他状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: StrictStr
    event_id: StrictStr
    agent_id: StrictStr
    expected_version: Annotated[int, Field(strict=True, ge=0)]
    needs_delta: NeedDelta

    @field_validator("proposal_id", "event_id", "agent_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """保留精确关联标识，拒绝空白或首尾空格。"""
        if not value.strip() or value != value.strip():
            raise ValueError("提案、事件与角色 ID 不能为空，也不能包含首尾空白")
        return value


class SettlementError(ValueError):
    """结算被拒绝，调用方应按具体原因处理。"""


class AgentMismatchError(SettlementError):
    """提案的目标角色与当前状态不一致。"""


class StateVersionConflictError(SettlementError):
    """提案预期版本与当前状态版本不一致，需要重新读取状态。"""


class SettlementTimeError(SettlementError):
    """结算时间无效，或早于当前状态的更新时间。"""


def settle_state(
    state: AgentState,
    proposal: StateChangeProposal,
    *,
    settled_at: datetime,
) -> AgentState:
    """基于已校验的输入生成新状态，不修改输入，也不保存任何内部状态。

    settled_at 由调用方明确提供，必须带时区，可以等于但不能早于当前
    updated_at。需求应用变化量后限制在 0～100；每次成功结算的候选状态
    版本递增 1，即使数值全部因边界限制而保持不变。

    此函数不检查事件是否存在、权限、提案是否已处理，也不执行数据库
    提交。结果由存储层在版本检查、去重与事务提交成功后正式生效。
    将同一旧状态再次传入仍能生成结果，因此不能单靠此函数保证幂等性。
    """
    if proposal.agent_id != state.agent_id:
        raise AgentMismatchError("提案目标角色与当前状态的角色不一致")
    if proposal.expected_version != state.version:
        raise StateVersionConflictError(
            f"状态版本不匹配：提案预期 {proposal.expected_version}，当前 {state.version}"
        )
    if not isinstance(settled_at, datetime) or settled_at.utcoffset() is None:
        raise SettlementTimeError("settled_at 必须是带时区的 datetime")

    settled_at_utc = settled_at.astimezone(timezone.utc)
    if settled_at_utc < state.updated_at:
        raise SettlementTimeError("结算时间不能早于当前状态的更新时间")

    need_values = {}
    for name in ("energy", "satiety", "boredom", "social_need"):
        current = getattr(state.needs, name)
        delta = getattr(proposal.needs_delta, name)
        need_values[name] = min(100.0, max(0.0, current + delta))

    # 正常构造模型以重新校验，不能使用跳过校验的 model_copy(update=...)。
    return AgentState(
        agent_id=state.agent_id,
        needs=NeedState(**need_values),
        updated_at=settled_at_utc,
        version=state.version + 1,
    )

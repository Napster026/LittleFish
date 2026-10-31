"""状态变更提案与内存结算；持久化、去重和事务由业务提交层协调。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from core.state import AgentState
from persona.needs import NeedDelta, NeedState, NeedTimeAdvance
from persona.emotion import EmotionChange


# noinspection PyNestedDecorators
class StateChangeProposal(BaseModel):
    """某个事件基于特定版本状态提出的需求、时间或情绪变更。

    proposal_id 由生产方分配，重试同一提案应沿用原 ID。event_id 关联
    依据事件，agent_id 指向目标角色。expected_version 必须明确提供，
    不能用默认值掩盖提案基于哪个状态版本计算。

    本模型只检查提案格式，不保证 ID 唯一、事件存在、角色有权限、版本
    匹配或提案已提交。这些检查交由结算与业务提交模块执行。当前支持
    四项需求变化、自然需求时间游标推进和可选情绪快照更新。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: StrictStr
    event_id: StrictStr
    agent_id: StrictStr
    expected_version: Annotated[int, Field(strict=True, ge=0)]
    needs_delta: NeedDelta | None
    needs_time: NeedTimeAdvance | None = None
    emotion_change: EmotionChange | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> StateChangeProposal:
        if self.needs_delta is None and self.needs_time is None and self.emotion_change is None:
            raise ValueError("提案必须包含需求变化、时间推进或情绪变更")
        if self.needs_time is not None and self.needs_time.from_at is None and self.needs_delta is not None:
            raise ValueError("首次建立需求时间基线不能补算未知历史")
        return self

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


class NeedTimeConflictError(SettlementError):
    """提案依据的需求时间游标与当前状态不一致。"""


class EmotionTimeConflictError(SettlementError):
    """情绪旧游标不匹配，或尝试覆盖已有基线/动力参数。"""


def settle_state(
    state: AgentState,
    proposal: StateChangeProposal,
    *,
    settled_at: datetime,
) -> AgentState:
    """基于已校验的输入生成新状态，不修改输入，也不保存任何内部状态。

    settled_at 由调用方明确提供，必须带时区，可以等于但不能早于当前
    updated_at。需求应用变化量后限制在 0～100；每次成功结算的候选状态
    版本递增 1，即使只有时间游标变化或数值全部已到边界。

    此函数不检查事件是否存在、权限、提案是否已处理，也不执行数据库
    提交。结果由业务提交层在版本检查、去重与事务提交成功后正式生效。
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

    needs_updated_at = state.needs_updated_at
    emotion = state.emotion
    if proposal.emotion_change is not None:
        change = proposal.emotion_change
        previous_time = None if emotion is None else emotion.updated_at
        if change.from_at != previous_time:
            raise EmotionTimeConflictError("情绪时间游标已经变化")
        if change.state.updated_at > settled_at_utc:
            raise SettlementTimeError("情绪时间游标不能晚于结算时间")
        if emotion is not None and (
            change.state.baseline != emotion.baseline or change.state.dynamics != emotion.dynamics
        ):
            raise EmotionTimeConflictError("情绪更新不能改变人格基线或动力参数")
        emotion = change.state
    if proposal.needs_time is not None:
        if proposal.needs_time.from_at != needs_updated_at:
            raise NeedTimeConflictError("需求时间游标已经变化，不能重复计算或跳过时间段")
        if proposal.needs_time.to_at > settled_at_utc:
            raise SettlementTimeError("需求时间游标不能晚于结算时间")
        needs_updated_at = proposal.needs_time.to_at

    need_values = {}
    for name in ("energy", "satiety", "boredom", "social_need"):
        current = getattr(state.needs, name)
        delta = 0.0 if proposal.needs_delta is None else getattr(proposal.needs_delta, name)
        need_values[name] = min(100.0, max(0.0, current + delta))

    # 正常构造模型以重新校验，不能使用跳过校验的 model_copy(update=...)。
    return AgentState(
        agent_id=state.agent_id,
        needs=NeedState(**need_values),
        updated_at=settled_at_utc,
        version=state.version + 1,
        needs_updated_at=needs_updated_at,
        emotion=emotion,
    )

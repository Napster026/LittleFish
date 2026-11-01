"""基于固定活动定义和完成事实构造需求变更提案，不访问数据库。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .models import ActivityRecord, ActivityStatus, ExecutionMode
from ..core.events import AgentEvent, EventProvenance
from ..core.settlement import StateChangeProposal
from ..core.state import AgentState
from ..persona.needs import NeedDelta


class ActivityRewardError(ValueError):
    """活动完成效果不满足结算条件。"""


class ActivityNotCompletedError(ActivityRewardError):
    """活动尚未完成，不能结算完成效果。"""


class InvalidCompletionEventError(ActivityRewardError):
    """完成事件与活动记录不一致或缺少可信结算依据。"""


def build_completion_proposal(
    record: ActivityRecord, event: AgentEvent, state: AgentState
) -> StateChangeProposal | None:
    """以原定义为效果来源；反馈只证明完成并校验，不任意决定变化量。

    此函数不去重、不写库；调用方必须验证记录与事件来自数据库，并在
    同一事务内完成版本检查与提交。没有配置完成效果时返回 None。
    """
    record = ActivityRecord.model_validate(record.model_dump())
    event = AgentEvent.model_validate(event.model_dump())
    state = AgentState.model_validate(state.model_dump())
    instance = record.instance
    if instance.status != ActivityStatus.COMPLETED:
        raise ActivityNotCompletedError("只有已完成活动可以结算完成效果")
    if state.agent_id != instance.agent_id:
        raise InvalidCompletionEventError("角色状态与活动所属角色不一致")
    if event.event_type != "ACTIVITY.completed" or event.activity_id != instance.activity_id:
        raise InvalidCompletionEventError("必须提供本次活动的完成事件")
    for key, expected in (
        ("agent_id", instance.agent_id), ("definition_id", instance.definition_id),
        ("mode", instance.mode.value), ("status", ActivityStatus.COMPLETED.value),
    ):
        if event.payload.get(key) != expected:
            raise InvalidCompletionEventError(f"完成事件的 {key} 不匹配")
    revision = event.payload.get("revision")
    if type(revision) is not int or revision != instance.revision:
        raise InvalidCompletionEventError("完成事件的修订号不匹配")
    progress = event.payload.get("progress")
    if type(progress) not in (int, float) or progress != 1.0:
        raise InvalidCompletionEventError("完成事件必须明确记录进度为 1")
    if not instance.created_at <= event.timestamp <= instance.updated_at:
        raise InvalidCompletionEventError("完成事件时间不在活动记录的时间范围内")
    expected_source = (
        EventProvenance.SIMULATED if instance.mode == ExecutionMode.SIMULATED
        else EventProvenance.OBSERVED
    )
    if event.provenance != expected_source:
        raise InvalidCompletionEventError("完成事件来源不符合本次执行模式")

    effect = record.definition.completion_needs_delta
    if "needs_delta" in event.payload:
        declared = event.payload["needs_delta"]
        try:
            supplied = None if declared is None else NeedDelta.model_validate(declared)
        except ValidationError as error:
            raise InvalidCompletionEventError("完成事件的需求效果格式不合法") from error
        if supplied != effect:
            raise InvalidCompletionEventError("完成事件的需求效果与原定义不一致")
    if effect is None:
        return None

    identity = json.dumps(
        ["persona.activity.completion", instance.agent_id, instance.activity_id,
         instance.revision, event.event_id],
        ensure_ascii=False, separators=(",", ":"),
    )
    return StateChangeProposal(
        proposal_id=str(uuid5(NAMESPACE_URL, identity)),
        event_id=event.event_id,
        agent_id=instance.agent_id,
        expected_version=state.version,
        needs_delta=effect,
    )

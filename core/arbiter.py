"""纯行为仲裁：先检查硬约束，再比较效用；不依赖活动执行或存储层。"""

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


class BehaviorKind(str, Enum):
    START_ACTIVITY = "start_activity"
    CONTINUE_ACTIVITY = "continue_activity"
    IDLE = "idle"


class ArbitrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    autonomous_enabled: StrictBool = True
    disabled_definition_ids: frozenset[StrictStr] = frozenset()

    @field_validator("disabled_definition_ids")
    @classmethod
    def nonempty_ids(cls, value):
        if any(not item.strip() or item != item.strip() for item in value):
            raise ValueError("禁用活动 ID 不能为空或包含首尾空白")
        return value


class ActiveBehavior(BaseModel):
    """应用层提供的占用快照，不将具体活动模型引入内核。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    activity_id: StrictStr = Field(min_length=1)
    definition_id: StrictStr = Field(min_length=1)
    mode: StrictStr = Field(min_length=1)
    status: Literal["pending", "running", "paused"]


class ArbitrationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    active: tuple[ActiveBehavior, ...] = ()
    recoverable_activity_ids: frozenset[StrictStr] = frozenset()

    @model_validator(mode="after")
    def validate_identity(self):
        ids = [a.activity_id for a in self.active]
        if len(ids) != len(set(ids)):
            raise ValueError("占用活动 ID 不能重复")
        pending = {a.activity_id for a in self.active if a.status == "pending"}
        if not self.recoverable_activity_ids <= pending:
            raise ValueError("可恢复标识必须引用当前 pending 活动")
        return self


class UtilityBreakdown(BaseModel):
    """可选的活动评分解释；内核不计算或读取具体情绪模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    need_score: Annotated[float, Field(allow_inf_nan=False)]
    emotion_adjustment: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
    final_score: Annotated[float, Field(allow_inf_nan=False)]

    @field_validator("need_score", "emotion_adjustment", "final_score", mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("评分分项必须是有限数值")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.need_score <= 0 and self.emotion_adjustment != 0:
            raise ValueError("没有正需求收益时不能加入情绪修正")
        if self.final_score != self.need_score + self.emotion_adjustment:
            raise ValueError("最终分必须等于需求分加情绪修正")
        return self


class BehaviorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: StrictStr = Field(min_length=1)
    kind: BehaviorKind
    utility: Annotated[float, Field(allow_inf_nan=False)] = 0.0
    definition_id: StrictStr | None = None
    activity_id: StrictStr | None = None
    mode: StrictStr | None = None
    unavailable_reason: StrictStr | None = None
    score_breakdown: UtilityBreakdown | None = None

    @field_validator("utility", mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("行为效用必须是有限数值")
        return value

    @model_validator(mode="after")
    def validate_target(self):
        if self.score_breakdown is not None and self.utility != self.score_breakdown.final_score:
            raise ValueError("候选效用必须等于评分分项的最终分")
        if self.kind == BehaviorKind.IDLE:
            if any(v is not None for v in (self.definition_id, self.activity_id, self.mode)):
                raise ValueError("空闲候选不能绑定活动")
        elif not self.definition_id or not self.mode:
            raise ValueError("活动候选必须指定定义和执行模式")
        if self.kind == BehaviorKind.CONTINUE_ACTIVITY and not self.activity_id:
            raise ValueError("继续活动必须指定实例")
        return self


class CandidateAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate: BehaviorCandidate
    allowed: StrictBool
    reason: StrictStr


class BehaviorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    winner: BehaviorCandidate
    reason: StrictStr
    assessments: tuple[CandidateAssessment, ...] = ()


class BehaviorArbiter:
    """当前只仲裁开始、继续和空闲；运行活动不抢占，暂停不恢复。

    输入为应用层生成的开始候选；继续与空闲由内核构建。接续已持久化
    的 pending 不重新比较需求分数，但仍遵守禁用、模式和占用约束。
    """

    def __init__(self, policy: ArbitrationPolicy | None = None):
        self.policy = ArbitrationPolicy.model_validate((policy or ArbitrationPolicy()).model_dump())

    def _reject(self, candidate: BehaviorCandidate, context: ArbitrationContext) -> str | None:
        if not self.policy.autonomous_enabled:
            return "autonomy_disabled"
        if candidate.definition_id in self.policy.disabled_definition_ids:
            return "activity_disabled"
        if candidate.mode != "simulated":
            return "unsupported_mode"
        if len(context.active) > 1:
            return "conflicting_activities"
        if candidate.activity_id is not None:
            if candidate.activity_id not in context.recoverable_activity_ids:
                return "not_recoverable"
            current = context.active[0]
            if (candidate.definition_id, candidate.mode) != (current.definition_id, current.mode):
                return "target_mismatch"
        elif context.active:
            return "activity_occupied"
        if candidate.unavailable_reason is not None:
            return candidate.unavailable_reason
        if candidate.activity_id is None and candidate.utility <= 0:
            return "nonpositive_utility"
        return None

    def decide(
        self, context: ArbitrationContext, candidates: tuple[BehaviorCandidate, ...],
    ) -> BehaviorDecision:
        context = ArbitrationContext.model_validate(context.model_dump())
        copies = tuple(BehaviorCandidate.model_validate(c.model_dump()) for c in candidates)
        if any(c.kind != BehaviorKind.START_ACTIVITY for c in copies):
            raise ValueError("候选来源只提供开始行为；继续和空闲由仲裁器构建")
        if len({c.candidate_id for c in copies}) != len(copies):
            raise ValueError("候选 ID 不能重复")
        if any(c.candidate_id == "idle" or c.candidate_id.startswith("continue:") for c in copies):
            raise ValueError("候选 ID 不能占用内核保留名称")
        assessments = []
        allowed = []
        for candidate in sorted(copies, key=lambda c: c.candidate_id):
            rejection = self._reject(candidate, context)
            assessments.append(CandidateAssessment(
                candidate=candidate, allowed=rejection is None, reason=rejection or "eligible",
            ))
            if rejection is None:
                allowed.append(candidate)
        idle = BehaviorCandidate(candidate_id="idle", kind=BehaviorKind.IDLE)
        assessments.append(CandidateAssessment(candidate=idle, allowed=True, reason="fallback"))
        if len(context.active) == 1 and context.active[0].status == "running":
            current = context.active[0]
            winner = BehaviorCandidate(
                candidate_id=f"continue:{current.activity_id}", kind=BehaviorKind.CONTINUE_ACTIVITY,
                activity_id=current.activity_id, definition_id=current.definition_id, mode=current.mode,
            )
            assessments.append(CandidateAssessment(candidate=winner, allowed=True, reason="no_preemption"))
            reason = "continue_running"
        elif allowed:
            winner = sorted(allowed, key=lambda c: (-c.utility, c.candidate_id))[0]
            reason = "recover_pending" if winner.activity_id is not None else "start_activity"
        else:
            winner = idle
            if len(context.active) > 1:
                reason = "conflicting_activities"
            elif context.active:
                reason = "paused" if context.active[0].status == "paused" else "pending_blocked"
            else:
                reason = "no_candidate" if self.policy.autonomous_enabled else "autonomy_disabled"
        return BehaviorDecision(winner=winner, reason=reason, assessments=tuple(assessments))

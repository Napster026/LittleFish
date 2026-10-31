"""持久评价预约与互斥结算。无远端请求；调用方复用已有模型调用提交结果。"""

import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from typing import Literal
from ..cognition.evaluation import EvaluationSettings, RuleEvaluation
from ..cognition.semantic import AppraisalInput, parse_experience
from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import ScheduledEvent, normalize_schedule_time
from ..persistence.errors import EventConflictError
from .evaluation import EVALUATION_EVENT_TYPE, evaluation_id, evaluate_experience
from .evaluation_persistence import EvaluationPersistence

APPRAISAL_SOURCE = "cognition.appraisal"
REQUEST_EVENT_TYPE = "INTERNAL.evaluation_requested"
DUE_EVENT_TYPE = "INTERNAL.evaluation_due"
READY_EVENT_TYPE = "INTERNAL.evaluation_ready"


def job_id(agent_id, source_id):
    return "appraisal-job:"+str(uuid5(NAMESPACE_URL, json.dumps([agent_id, source_id], separators=(",", ":"))))


class AppraisalJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1, 2] = 1
    request: AppraisalInput
    settings: EvaluationSettings
    fallback: RuleEvaluation
    created_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def deadline_matches_settings(self):
        if self.deadline != self.created_at+timedelta(seconds=self.settings.semantic.timeout_seconds):
            raise ValueError("评价截止时间必须匹配预约配置")
        if not (self.settings.enabled and self.settings.activity_feedback_enabled and self.settings.semantic.enabled):
            raise ValueError("仅启用语义评价的活动可建立预约")
        return self

    @property
    def identifier(self):
        return job_id(self.request.agent_id, self.request.source_event_id)

    def event(self):
        return AgentEvent(event_id=self.identifier, event_type=REQUEST_EVENT_TYPE,
            timestamp=self.created_at, source=APPRAISAL_SOURCE, provenance=EventProvenance.INFERRED,
            activity_id=self.request.activity_id, causation_id=self.request.source_event_id,
            payload={"agent_id": self.request.agent_id, "job": self.model_dump(mode="json")})

    def schedule(self):
        identifier = "appraisal-due:"+self.identifier.removeprefix("appraisal-job:")
        event = AgentEvent(event_id=identifier, event_type=DUE_EVENT_TYPE, timestamp=self.deadline,
            source=APPRAISAL_SOURCE, provenance=EventProvenance.OBSERVED,
            activity_id=self.request.activity_id, causation_id=self.request.source_event_id,
            payload={"agent_id": self.request.agent_id, "source_event_id": self.request.source_event_id})
        return ScheduledEvent(schedule_id=identifier, agent_id=self.request.agent_id, event=event, due_at=self.deadline)


class AppraisalPersistence:
    def __init__(self, core, commit_emotion, current_settings):
        self.core = core
        self.evaluator = EvaluationPersistence(core, commit_emotion)
        self.current_settings = current_settings

    async def get(self, agent_id, source_id):
        event = await self.core.get_event(job_id(agent_id, source_id))
        if event is None:
            return None
        job = AppraisalJob.model_validate(event.payload["job"])
        if (event.model_dump(exclude={"payload"}) != job.event().model_dump(exclude={"payload"})
                or set(event.payload) != {"agent_id", "job"} or event.payload["agent_id"] != agent_id
                or job.request.agent_id != agent_id or job.request.source_event_id != source_id):
            raise ValueError("评价预约身份不一致")
        return job

    async def begin(self, source, request, settings, fallback, *, now):
        now = normalize_schedule_time(now)
        if now < source.timestamp or request.source_event_id != source.event_id:
            raise ValueError("评价预约时间或原事件不一致")
        previous = await self.core.get_event(evaluation_id(request.agent_id, source.event_id))
        if previous is not None:
            return previous
        existing = await self.get(request.agent_id, source.event_id)
        if existing is not None:
            return existing.event()
        if (now-source.timestamp).total_seconds() > settings.max_effect_age_seconds:
            return await self.evaluator.evaluate_once(request.agent_id, source, settings,
                RuleEvaluation(reason="evaluation_expired"), now=now, layer="expired")
        job = AppraisalJob(version=2, request=request, settings=settings, fallback=fallback, created_at=now,
                           deadline=now+timedelta(seconds=settings.semantic.timeout_seconds))
        await self.core.record_event(job.event())
        await self.core.create_schedule(job.schedule())
        return job.event()

    def effective_settings(self, job):
        data = job.settings.model_dump()
        current = self.current_settings
        if current is not None:
            data["max_axis_delta"] = min(job.settings.max_axis_delta, current.max_axis_delta)
            data["max_effect_age_seconds"] = min(job.settings.max_effect_age_seconds, current.max_effect_age_seconds)
            data["needs"]["max_delta"] = min(job.settings.needs.max_delta, current.needs.max_delta)
            data["needs"]["enabled"] = job.settings.needs.enabled and current.needs.enabled
        if job.version == 1:
            data["needs"]["enabled"] = False  # 旧预约不追溯发放新版本心理收益。
        return EvaluationSettings(**data)

    async def _finish(self, job, result, *, now, layer, experience=None):
        source = await self.core.get_event(job.request.source_event_id)
        if source is None:
            raise ValueError("评价缺少原始反馈")
        settings = self.current_settings
        if settings is None or not settings.enabled or not settings.activity_feedback_enabled or not settings.semantic.enabled:
            result = RuleEvaluation(reason="evaluation_disabled_after_request")
            experience = None
        effective = self.effective_settings(job)
        if result.impulse is not None:
            result = RuleEvaluation(**{**result.model_dump(), "impulse": effective.bounded(result.impulse)})
        return await self.evaluator.evaluate_once(job.request.agent_id, source, effective,
            result, now=now, layer=layer, experience=experience)

    async def fallback(self, job, *, now, reason):
        result = RuleEvaluation(**{**job.fallback.model_dump(), "reason": reason})
        return await self._finish(job, result, now=now, layer="fallback")

    async def due(self, event, *, now):
        now = normalize_schedule_time(now)
        agent, source_id = event.payload.get("agent_id"), event.payload.get("source_event_id")
        job = await self.get(agent, source_id)
        if job is None or event != job.schedule().event or now < job.deadline:
            raise ValueError("评价超时事件缺少匹配预约或尚未到期")
        plan = await self.core.get_schedule(agent, event.event_id)
        if plan is None or plan.schedule != job.schedule():
            raise ValueError("评价超时事件缺少持久计划")
        await self.core.record_event(event)
        return await self.fallback(job, now=now, reason="semantic_timeout")

    async def complete(self, agent, source_id, raw, *, now):
        now = normalize_schedule_time(now)
        job = await self.get(agent, source_id)
        if job is None:
            raise ValueError("原反馈没有等待语义评价的预约")
        if now < job.created_at:
            raise ValueError("评价结果早于预约")
        receipt = await self.core.get_event(evaluation_id(agent, source_id))
        if receipt is None and now >= job.deadline:
            receipt = await self.fallback(job, now=now, reason="semantic_timeout")
        try:
            experience = parse_experience(raw)
        except (ValueError, TypeError):
            return await self.fallback(job, now=now, reason="semantic_invalid")
        ready_id = "appraisal-ready:"+job.identifier.removeprefix("appraisal-job:")
        previous_ready = await self.core.get_event(ready_id)
        if previous_ready is not None:
            if previous_ready.payload.get("experience") != experience.model_dump(mode="json"):
                raise EventConflictError("同一预约的模型结果不能改写")
            previous_receipt = await self.core.get_event(evaluation_id(agent, source_id))
            if previous_receipt is None:
                raise ValueError("已有分析结果缺少原子提交的最终回执")
            return previous_receipt
        ready = AgentEvent(event_id=ready_id, event_type=READY_EVENT_TYPE, timestamp=now,
            source=APPRAISAL_SOURCE, provenance=EventProvenance.INFERRED,
            activity_id=job.request.activity_id, causation_id=source_id,
            payload={"agent_id": agent, "experience": experience.model_dump(mode="json"),
                     "disposition": "late" if receipt is not None else "accepted"})
        await self.core.record_event(ready)
        if receipt is not None:
            return receipt  # 迟到结果只保存分析，不再结算情绪。
        if experience.uncertain:
            return await self.fallback(job, now=now, reason="semantic_uncertain")
        result = evaluate_experience(job.request, experience, self.effective_settings(job), prior=job.fallback)
        return await self._finish(job, result, now=now, layer="semantic", experience=experience)

    async def pending(self, agent_id, *, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("查询上限须为 1～100")
        found, after = [], None
        while len(found) < limit:
            # 仅查询当前待投递计划；不会解析随运行时间增长的历史事件。
            rows = await self.core.session.fetch_all("scheduled_events",
                where={"agent_id": agent_id, "status": "pending"}, order_by=("schedule_id",),
                after=None if after is None else {"schedule_id": after})
            if not rows: break
            for row in rows:
                plan = self.core.schedule_record(row)
                event = plan.schedule.event
                if event.event_type != DUE_EVENT_TYPE or event.source != APPRAISAL_SOURCE:
                    continue
                source_id = event.payload.get("source_event_id")
                if await self.core.get_event(evaluation_id(agent_id, source_id)) is not None:
                    continue
                job = await self.get(agent_id, source_id)
                if job is None or plan.schedule != job.schedule():
                    raise ValueError("待评价计划缺少匹配预约")
                found.append(job)
                if len(found) == limit: break
            after = rows[-1]["schedule_id"]
        return tuple(found)

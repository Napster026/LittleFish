"""统一最终回执、聊天冷却和情绪提交；规则与语义结果共享原事件去重键。"""

import json
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from ..cognition.evaluation import RuleEvaluation
from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import normalize_schedule_time
from ..core.settlement import StateChangeProposal
from .emotion import build_emotion_impulse, build_emotion_proposal
from .evaluation import EVALUATION_EVENT_TYPE, EVALUATION_SOURCE, evaluation_id


class EvaluationPersistence:
    def __init__(self, core, commit_emotion):
        self.core, self.commit_emotion = core, commit_emotion

    async def _cooldown(self, agent_id, key, seconds, now):
        bucket = int(now.timestamp() // seconds)
        def anchor(index):
            identity = json.dumps([agent_id, key, seconds, index], separators=(",", ":"))
            return f"rule-cooldown:{uuid5(NAMESPACE_URL, identity)}"
        for index in (bucket, bucket-1):
            old = await self.core.get_event(anchor(index))
            if old is not None and datetime.fromisoformat(old.payload["until"]) > now:
                return None
        return anchor(bucket)

    async def _anchor(self, identifier, source, agent_id, seconds, now):
        if identifier is not None:
            await self.core.record_event(AgentEvent(
                event_id=identifier, event_type="INTERNAL.rule_cooldown", timestamp=now,
                source=EVALUATION_SOURCE, provenance=EventProvenance.INFERRED,
                scope_id=source.scope_id, causation_id=source.event_id,
                payload={"agent_id": agent_id, "until": (now+timedelta(seconds=seconds)).isoformat()},
            ))

    async def evaluate_once(self, agent_id, source, settings, result, *, now, cooldown_key=None,
                            layer="rule", experience=None):
        now = normalize_schedule_time(now)
        identifier = evaluation_id(agent_id, source.event_id)
        previous = await self.core.get_event(identifier)
        if previous is not None:
            if (previous.event_type != EVALUATION_EVENT_TYPE or previous.source != EVALUATION_SOURCE
                    or previous.payload.get("agent_id") != agent_id or previous.causation_id != source.event_id):
                raise ValueError("评价回执身份冲突")
            return previous
        state = await self.core.get_state(agent_id)
        if state is None or now < state.updated_at or now < source.timestamp:
            raise ValueError("角色不存在或评价时间早于当前状态/原事件")
        age = (now-source.timestamp).total_seconds()
        if age > settings.max_effect_age_seconds:
            result = RuleEvaluation(rule_id=result.rule_id, reason="evaluation_expired", needs_reason="expired")
            layer = "expired"
        if state.emotion is None and result.impulse is not None:
            result = RuleEvaluation(**{**result.model_dump(), "impulse": None, "reason": "emotion_uninitialized"})
        anchor_id = None
        if (result.impulse is not None or result.needs_delta is not None) and cooldown_key and settings.cooldown_seconds > 0:
            anchor_id = await self._cooldown(agent_id, cooldown_key, settings.cooldown_seconds, now)
            if anchor_id is None:
                result = RuleEvaluation(rule_id=result.rule_id, reason="repeat_cooldown", needs_reason="repeat_cooldown")
        need_anchor = None
        if result.needs_delta is not None:
            if not settings.needs.enabled:
                result = RuleEvaluation(**{**result.model_dump(), "needs_delta": None, "needs_reason": "disabled"})
            else:
                bounded = settings.needs.bounded(result.needs_delta.model_dump())
                values = {axis: max(-getattr(state.needs, axis), min(100-getattr(state.needs, axis), getattr(bounded, axis)))
                          for axis in ("boredom", "social_need")}
                needs = settings.needs.bounded(values)
                if needs is None:
                    result = RuleEvaluation(**{**result.model_dump(), "needs_delta": None, "needs_reason": "at_boundary"})
                else:
                    key = ("need-activity:"+source.activity_id if source.activity_id is not None
                           else "need-chat:"+json.dumps([source.scope_id, source.payload.get("sender_id")]))
                    need_anchor = await self._cooldown(agent_id, key, settings.needs.cooldown_seconds, now)
                    result = RuleEvaluation(**{**result.model_dump(), "needs_delta": needs if need_anchor else None,
                        "needs_reason": result.needs_reason if need_anchor else "need_cooldown"})
        if result.impulse is not None:
            event = build_emotion_impulse(agent_id, source, result.impulse)
            await self.core.record_event(event)
            committed = await self.core.get_committed_proposal(agent_id, event.event_id)
            if committed is not None and committed.needs_delta != result.needs_delta:
                result = RuleEvaluation(**{**result.model_dump(), "needs_delta": committed.needs_delta,
                                          "needs_reason": "existing_settlement"})
                need_anchor = None
            proposal = committed or build_emotion_proposal(event, state, to_at=now)
            if committed is None and result.needs_delta is not None:
                proposal = StateChangeProposal(**{**proposal.model_dump(), "needs_delta": result.needs_delta})
            await self.commit_emotion(self.core, proposal, settled_at=now, evaluation_needs=result.needs_delta)
        elif result.needs_delta is not None:
            event = AgentEvent(event_id="evaluation-needs:"+identifier.removeprefix("rule-evaluation:"),
                event_type="INTERNAL.evaluation_need_effect", timestamp=source.timestamp,
                source=EVALUATION_SOURCE, provenance=EventProvenance.INFERRED,
                scope_id=source.scope_id, activity_id=source.activity_id, causation_id=source.event_id,
                payload={"agent_id": agent_id, "needs_delta": result.needs_delta.model_dump(mode="json")})
            await self.core.record_event(event)
            proposal = StateChangeProposal(proposal_id="proposal:"+event.event_id, event_id=event.event_id,
                agent_id=agent_id, expected_version=state.version, needs_delta=result.needs_delta)
            await self.core.commit_proposal(proposal, settled_at=now)
        if result.impulse is not None or result.needs_delta is not None:
            await self._anchor(anchor_id, source, agent_id, settings.cooldown_seconds, now)
        if result.needs_delta is not None:
            await self._anchor(need_anchor, source, agent_id, settings.needs.cooldown_seconds, now)
        receipt = AgentEvent(
            event_id=identifier, event_type=EVALUATION_EVENT_TYPE, timestamp=now, source=EVALUATION_SOURCE,
            provenance=EventProvenance.INFERRED, scope_id=source.scope_id, activity_id=source.activity_id,
            causation_id=source.event_id, payload={"agent_id": agent_id, "rule_version": 3, "layer": layer,
                "experience": None if experience is None else experience.model_dump(mode="json"),
                "evaluation": result.model_dump(mode="json"), "max_axis_delta": settings.max_axis_delta,
                "cooldown_seconds": settings.cooldown_seconds, "effect_age_seconds": age,
                "max_effect_age_seconds": settings.max_effect_age_seconds},
        )
        await self.core.record_event(receipt)
        return receipt

    async def latest(self, agent_id):
        latest, after = None, None
        def order(item):
            return (item.timestamp, item.event_type == EVALUATION_EVENT_TYPE, item.event_id)
        while True:
            rows = await self.core.session.fetch_all("agent_events", after=None if after is None else {"event_id": after},
                                                      order_by=("event_id",))
            if not rows:
                return latest
            for row in rows:
                event = AgentEvent.model_validate_json(row["event_json"])
                accepted = ((event.event_type == EVALUATION_EVENT_TYPE and event.source == EVALUATION_SOURCE)
                            or (event.event_type == "INTERNAL.evaluation_requested" and event.source == "cognition.appraisal"))
                if (accepted and event.payload.get("agent_id") == agent_id
                        and (latest is None or order(event) > order(latest))):
                    latest = event
            after = rows[-1]["event_id"]

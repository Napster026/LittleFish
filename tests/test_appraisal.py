"""统一评价与互斥结算；由开发者手动运行，不访问模型。"""

import asyncio
import json
import sqlite3
from contextlib import closing
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.feedback import ActivityFeedback, attach_completion_feedback, build_feedback_event
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ActivityStatus, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.service import ActivityService
from persona_agent.application.appraisal_persistence import job_id
from persona_agent.application.evaluation import evaluation_id, evaluate_activity_event
from persona_agent.application.persistence import PersonaStore
from persona_agent.cognition.evaluation import EvaluationSettings, RuleEvaluation
from persona_agent.cognition.semantic import ExperienceAssessment, parse_activity_analysis, parse_experience
from persona_agent.core.events import AgentEvent
from persona_agent.core.persistence import CorePersistence
from persona_agent.core.settlement import StateChangeProposal
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persistence.errors import EventConflictError
from persona_agent.persona.emotion import EmotionDynamics, EmotionState, VAD
from persona_agent.persona.needs import NeedState

TIME = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def assessment(**changes):
    data = dict(interest=1, frustration=0, progress=0, control=0, pleasantness=0,
                activation=0, importance=.6, uncertain=False)
    data.update(changes)
    return ExperienceAssessment(**data)


class SemanticProtocolTests(unittest.TestCase):
    def test_strict_output_and_independent_facts(self):
        parsed = parse_activity_analysis(json.dumps({"summary": "输了但很有趣", "experience": assessment().model_dump()}))
        feedback = ActivityFeedback(checkpoint_id="one", task="failure", summary=parsed.summary, experience=parsed.experience)
        self.assertEqual(feedback.task, "failure")
        for extra in ({"task": "success"}, {"needs": {"energy": 10}}, {"status": "completed"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                parse_experience({**assessment().model_dump(), **extra})
        with self.assertRaises(ValidationError): assessment(interest=True)
        with self.assertRaises(ValidationError): assessment(importance=float("nan"))
        with self.assertRaises(ValueError): parse_experience('{"interest":0,"interest":1}')
        with self.assertRaises(ValidationError):
            ActivityFeedback(checkpoint_id="one", outcome="failure", task="failure", summary="不能混用")


class AppraisalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.agent = "persona-001"
        self.now = TIME
        self.store = PersonaStore(SQLiteStorage(Path(self.directory.name)/"appraisal.sqlite3"),
                                  evaluation=EvaluationSettings(semantic={"enabled": True, "timeout_seconds": 10}))
        await self.store.open()
        zero = VAD(valence=0, arousal=0, dominance=0)
        self.initial = AgentState(agent_id=self.agent, updated_at=TIME,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
            emotion=EmotionState(current=zero, mood=zero, baseline=zero, updated_at=TIME,
                dynamics=EmotionDynamics(emotion_half_life_seconds=300, mood_half_life_seconds=7200, mood_impulse_ratio=.1)))
        await self.store.create_state(self.initial)
        self.definition = ActivityDefinition(definition_id="play", name="模拟游戏", supported_modes=["simulated"],
                                             estimated_duration_seconds=10, completion_needs_delta={"energy": 10})
        instance = create_activity_instance(self.definition, activity_id="play-one", agent_id=self.agent,
                                           mode=ExecutionMode.SIMULATED, created_at=TIME)
        await self.store.create_activity(self.definition, instance)
        registry = ExecutorRegistry()
        registry.register("play", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.manager = ActivityManager(registry)
        self.service = ActivityService(self.agent, self.store, self.manager, clock=lambda: self.now)
        self.running = await self.service.start_activity("play-one")

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def state(self): return await self.store.get_state(self.agent)

    async def report(self, checkpoint="one", **changes):
        values = dict(checkpoint_id=checkpoint, task="failure", summary="输了三局，但过程很有趣", evaluation_mode="deferred")
        values.update(changes)
        return await self.store.report_activity_feedback(self.agent, "play-one", ActivityFeedback(**values), now=self.now)

    async def test_same_call_experience_can_be_positive_despite_task_failure(self):
        source = await self.report(experience=assessment(), evaluation_mode="immediate")
        state = await self.state()
        self.assertGreater(state.emotion.current.valence, 0)
        self.assertLess(state.emotion.current.dominance, 0)
        self.assertEqual(source.payload["feedback"]["task"], "failure")
        self.assertEqual(state.needs.energy, self.initial.needs.energy)
        self.assertEqual(state.needs.satiety, self.initial.needs.satiety)
        self.assertEqual(state.needs.social_need, self.initial.needs.social_need)
        self.assertEqual(state.needs.boredom, 28)
        self.assertEqual(await self.store.pending_appraisals(self.agent), ())
        self.assertEqual((await self.store.latest_evaluation(self.agent)).payload["layer"], "semantic")

    async def test_pending_restart_latest_state_and_result_replay(self):
        source = await self.report()
        self.assertEqual(await self.state(), self.initial)
        self.assertEqual(await self.store.get_activity(self.agent, "play-one"), self.running)
        self.assertIsNone(await self.store.get_event(evaluation_id(self.agent, source.event_id)))
        await self.store.close()
        await self.store.open()
        pending = await self.store.pending_appraisals(self.agent)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request.task, "failure")
        self.assertEqual(pending[0].request.text, "输了三局，但过程很有趣")
        effect = AgentEvent(event_id="need-change", event_type="INTERNAL.need_effect", timestamp=self.now,
                            source="test", provenance="observed", payload={})
        await self.store.record_event(effect)
        await self.store.commit_proposal(StateChangeProposal(proposal_id="need-proposal", event_id=effect.event_id,
            agent_id=self.agent, expected_version=0, needs_delta={"energy": 5}), settled_at=self.now)
        self.now += timedelta(seconds=1)
        receipt = await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        state = await self.state()
        self.assertEqual(state.needs.energy, 45)
        self.assertEqual(state.version, 2)
        self.assertEqual(receipt.payload["layer"], "semantic")
        self.assertEqual(await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now), receipt)
        self.assertEqual(await self.state(), state)
        self.assertEqual(await self.store.pending_appraisals(self.agent), ())
        with self.assertRaises(EventConflictError):
            await self.store.complete_appraisal(self.agent, source.event_id, assessment(interest=.2), now=self.now)

    async def test_deadline_and_result_race_has_only_one_fallback(self):
        source = await self.report()
        job = await self.store.get_appraisal(self.agent, source.event_id)
        self.now = job.deadline
        first, second = await asyncio.gather(
            self.store.handle_appraisal_deadline(self.agent, job.schedule().event, now=self.now),
            self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now))
        self.assertEqual(first, second)
        self.assertEqual(first.payload["layer"], "fallback")
        state = await self.state()
        self.assertEqual(state.version, 1)
        self.assertEqual(state.emotion.current.valence, -.04)
        self.assertEqual(state.needs, self.initial.needs)
        self.now += timedelta(seconds=1)
        self.assertEqual(await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now), first)
        self.assertEqual(await self.state(), state)

    async def test_invalid_and_uncertain_results_use_fallback_and_late_analysis_has_no_effect(self):
        for checkpoint, raw in (("invalid", '{"unknown":1}'), ("uncertain", assessment(uncertain=True))):
            source = await self.report(checkpoint)
            before = await self.state()
            receipt = await self.store.complete_appraisal(self.agent, source.event_id, raw, now=self.now)
            self.assertEqual(receipt.payload["layer"], "fallback")
            state = await self.state()
            self.assertEqual(state.version, before.version+1)
            if checkpoint == "invalid":
                await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
                self.assertEqual(await self.state(), state)

    async def test_completion_rewards_do_not_wait_for_semantics(self):
        self.now += timedelta(seconds=10)
        context = ActivityContext(definition=self.definition, instance=self.running.instance, now=self.now)
        result = await self.manager.step(context)
        feedback = ActivityFeedback(checkpoint_id="whole", scope="completion", task="failure",
                                    summary="整局输了，但过程很有趣", evaluation_mode="deferred")
        events = tuple(attach_completion_feedback(event, feedback) for event in result.events)
        record = await self.store.commit_activity_result("step", context,
            ActivityExecutionResult(instance=result.instance, events=events), settled_at=self.now)
        self.assertEqual(record.instance.status, ActivityStatus.COMPLETED)
        self.assertEqual((await self.state()).needs.energy, 50)
        self.assertEqual((await self.state()).emotion.current.valence, 0)
        self.assertEqual((await self.store.latest_activity_feedback(self.agent)).event_id, events[0].event_id)
        await self.store.complete_appraisal(self.agent, events[0].event_id, assessment(), now=self.now)
        self.assertEqual((await self.state()).needs.energy, 50)
        self.assertGreater((await self.state()).emotion.current.valence, 0)

    async def test_request_and_result_writes_roll_back_atomically(self):
        with patch.object(CorePersistence, "create_schedule", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError): await self.report("rollback")
        self.assertEqual(await self.store.pending_appraisals(self.agent), ())
        self.assertEqual(await self.state(), self.initial)
        source = await self.report()
        with patch.object(CorePersistence, "create_schedule", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual(await self.state(), self.initial)
        self.assertIsNone(await self.store.get_event(evaluation_id(self.agent, source.event_id)))
        ready = "appraisal-ready:"+job_id(self.agent, source.event_id).removeprefix("appraisal-job:")
        self.assertIsNone(await self.store.get_event(ready))
        await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual((await self.state()).version, 1)

    async def test_semantics_disabled_has_immediate_fallback_and_cancels_pending_effect(self):
        source = await self.report()
        self.store._evaluation = EvaluationSettings()
        receipt = await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual(receipt.payload["evaluation"]["reason"], "evaluation_disabled_after_request")
        self.assertEqual(await self.state(), self.initial)
        next_source = await self.report("disabled")
        receipt = await self.store.get_event(evaluation_id(self.agent, next_source.event_id))
        self.assertEqual(receipt.payload["layer"], "fallback")
        self.assertEqual((await self.state()).version, 1)

    async def test_new_global_limit_is_honored_for_pending_job(self):
        source = await self.report()
        self.store._evaluation = EvaluationSettings(max_axis_delta=.01, semantic={"enabled": True})
        receipt = await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual(receipt.payload["max_axis_delta"], .01)
        self.assertLessEqual(abs((await self.state()).emotion.current.valence), .01)
        self.assertEqual((await self.state()).emotion.current.dominance, -.01)

    async def test_exhausted_deadline_submission_can_recover(self):
        source = await self.report()
        job = await self.store.get_appraisal(self.agent, source.event_id)
        plan = await self.store.get_schedule(self.agent, job.schedule().schedule_id)
        for attempt in range(plan.schedule.max_attempts):
            self.now = plan.next_attempt_at
            plan = await self.store.record_schedule_delivery(self.agent, plan.schedule.schedule_id,
                expected_attempts=attempt, attempted_at=self.now, succeeded=False, error="persist failed")
        self.assertEqual(await self.state(), self.initial)
        await self.store.close()
        await self.store.open()
        await self.store.recover_appraisals(self.agent, now=self.now)
        state = await self.state()
        self.assertEqual(state.version, 1)
        self.assertEqual(state.emotion.current.valence, -.04)
        await self.store.recover_appraisals(self.agent, now=self.now)
        self.assertEqual(await self.state(), state)

    async def test_zero_semantics_preserves_rule_prior_and_high_strength_does_not_clip_flat(self):
        no_evidence = assessment(interest=0)
        fallback_event = build_feedback_event(self.running,
            ActivityFeedback(checkpoint_id="prior", task="failure", summary="任务失败"), at=self.now)
        semantic_event = build_feedback_event(self.running,
            ActivityFeedback(checkpoint_id="semantic", task="failure", summary="任务失败", experience=no_evidence), at=self.now)
        settings = self.store._evaluation
        rule = evaluate_activity_event(self.running, fallback_event, settings)
        semantic = evaluate_activity_event(self.running, semantic_event, settings)
        self.assertEqual(rule.impulse, semantic.impulse)
        outcomes = []
        for level in (.5, 1):
            event = build_feedback_event(self.running, ActivityFeedback(checkpoint_id=f"strength-{level}", task="failure",
                summary="有进展且愉快", experience=assessment(interest=level, progress=level,
                control=level, pleasantness=level, activation=level)), at=self.now)
            outcomes.append(evaluate_activity_event(self.running, event, settings).impulse.valence)
        self.assertLess(outcomes[0], outcomes[1])
        self.assertLess(outcomes[1], settings.max_axis_delta)

    async def test_psychological_needs_share_one_settlement_and_have_cooldown(self):
        source = await self.report(experience=assessment(), evaluation_mode="immediate")
        first = await self.state()
        self.assertEqual(first.needs.boredom, 28)
        self.assertEqual(first.needs.energy, 40)
        self.assertEqual(first.needs.satiety, 70)
        self.assertEqual(first.version, 1)
        self.now += timedelta(seconds=1)
        second = await self.report("second", experience=assessment(), evaluation_mode="immediate")
        receipt = await self.store.get_event(evaluation_id(self.agent, second.event_id))
        self.assertEqual(receipt.payload["evaluation"]["needs_reason"], "need_cooldown")
        self.assertEqual((await self.state()).needs.boredom, 28)
        self.now += timedelta(seconds=30)
        await self.report("third", experience=assessment(), evaluation_mode="immediate")
        state = await self.state()
        self.assertEqual(state.needs.boredom, 26)
        self.assertEqual((await self.store.get_event(evaluation_id(self.agent, source.event_id))).payload["evaluation"]["needs_delta"]["boredom"], -2)

    async def test_social_need_requires_structured_social_context(self):
        sample = assessment(interest=0, pleasantness=1)
        await self.report("solo", task="neutral", experience=sample, evaluation_mode="immediate")
        self.assertEqual((await self.state()).needs.social_need, 50)
        await self.report("social", task="neutral", experience=sample, evaluation_mode="immediate",
                          context={"social_interaction": True})
        self.assertEqual((await self.state()).needs.social_need, 49)
        with self.assertRaises(ValidationError):
            RuleEvaluation(reason="bad", needs_delta={"energy": 1})

    async def test_needs_only_effect_does_not_require_emotion_change(self):
        data = self.store._evaluation.model_dump()
        data["semantic"]["weights"] = {name: dict(valence=0, arousal=0, dominance=0)
            for name in ("interest", "frustration", "progress", "control", "pleasantness", "activation")}
        self.store._evaluation = EvaluationSettings(**data)
        source = await self.report(task="neutral", experience=assessment(), evaluation_mode="immediate")
        state = await self.state()
        self.assertEqual(state.emotion, self.initial.emotion)
        self.assertEqual(state.needs.boredom, 28)
        self.assertEqual(state.version, 1)
        receipt = await self.store.get_event(evaluation_id(self.agent, source.event_id))
        self.assertIsNone(receipt.payload["evaluation"]["impulse"])

    async def test_fixed_completion_axis_is_not_rewarded_again_by_appraisal(self):
        definition = ActivityDefinition(**{**self.definition.model_dump(), "completion_needs_delta": {"boredom": -10}})
        instance = create_activity_instance(definition, activity_id="reserved", agent_id=self.agent,
                                           mode=ExecutionMode.SIMULATED, created_at=self.now)
        await self.store.create_activity(definition, instance)
        service = ActivityService(self.agent, self.store, self.manager, clock=lambda: self.now)
        await service.start_activity("reserved")
        feedback = ActivityFeedback(checkpoint_id="interest", task="neutral", summary="有趣", experience=assessment())
        source = await self.store.report_activity_feedback(self.agent, "reserved", feedback, now=self.now)
        receipt = await self.store.get_event(evaluation_id(self.agent, source.event_id))
        self.assertEqual(receipt.payload["evaluation"]["needs_reason"], "completion_owned")
        self.assertEqual((await self.state()).needs.boredom, 30)
        self.now += timedelta(seconds=10)
        await service.pause_activity("reserved")
        self.assertEqual((await self.state()).needs.boredom, 20)

    async def test_two_hour_old_failed_deadline_only_archives_no_state_effect(self):
        source = await self.report()
        job = await self.store.get_appraisal(self.agent, source.event_id)
        plan = await self.store.get_schedule(self.agent, job.schedule().schedule_id)
        for attempt in range(plan.schedule.max_attempts):
            self.now = plan.next_attempt_at
            plan = await self.store.record_schedule_delivery(self.agent, plan.schedule.schedule_id,
                expected_attempts=attempt, attempted_at=self.now, succeeded=False, error="write failed")
        self.now = TIME+timedelta(hours=2)
        await self.store.close()
        await self.store.open()
        await self.store.recover_appraisals(self.agent, now=self.now)
        self.assertEqual(await self.state(), self.initial)
        receipt = await self.store.get_event(evaluation_id(self.agent, source.event_id))
        self.assertEqual(receipt.payload["layer"], "expired")
        self.assertEqual(receipt.payload["evaluation"]["reason"], "evaluation_expired")
        await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual(await self.state(), self.initial)

    async def test_pending_query_uses_live_schedule_index_not_event_history_scan(self):
        source = await self.report()
        async with self.store._backend.transaction() as session:
            session_type = type(session)
            for index in range(250):
                await session.insert("agent_events", {"event_id": f"history-{index}", "event_json": "not parsed by pending"})
        original = session_type.fetch_all
        async def guarded(session, table, **kwargs):
            if table == "agent_events" and set(kwargs.get("where", {})) != {"event_id"}:
                raise AssertionError("pending queried event history")
            return await original(session, table, **kwargs)
        with patch.object(session_type, "fetch_all", new=guarded):
            pending = await self.store.pending_appraisals(self.agent)
        self.assertEqual(pending[0].request.source_event_id, source.event_id)
        path = Path(self.directory.name)/"appraisal.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute("EXPLAIN QUERY PLAN SELECT * FROM scheduled_events WHERE agent_id=? AND status=? "
                "AND schedule_id>? ORDER BY schedule_id LIMIT ?", (self.agent,"pending","",20)).fetchall()
        self.assertTrue(any("scheduled_pending_idx" in row[3] for row in rows))

    async def test_v030_job_loads_without_retroactive_need_effect(self):
        source = await self.report()
        identifier = job_id(self.agent, source.event_id)
        event = await self.store.get_event(identifier)
        data = event.model_dump(mode="json")
        job = data["payload"]["job"]
        job.pop("version")
        job["settings"].pop("needs")
        job["settings"].pop("max_effect_age_seconds")
        job["settings"]["semantic"].pop("adjustment_scale")
        job["settings"]["semantic"]["weights"]["task_failure"] = dict(valence=0,arousal=0,dominance=-.03)
        job["fallback"].pop("needs_delta")
        job["fallback"].pop("needs_reason")
        job["request"].pop("reserved_needs")
        job["request"]["context"].pop("social_interaction")
        stored = json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",", ":"))
        async with self.store._backend.transaction() as session:
            await session.update("agent_events", {"event_json": stored}, where={"event_id": identifier})
        self.assertEqual((await self.store.get_appraisal(self.agent, source.event_id)).version, 1)
        await self.store.complete_appraisal(self.agent, source.event_id, assessment(), now=self.now)
        self.assertEqual((await self.state()).needs, self.initial.needs)
        self.assertEqual((await self.store.get_event(identifier)).model_dump(mode="json"), data)

    async def test_chat_social_need_effect_and_emotion_use_one_proposal(self):
        source = AgentEvent(event_id="chat-one", event_type="SOCIAL.message_received", timestamp=self.now,
            source="test", provenance="observed", scope_id="friend",
            payload={"text":"谢谢你", "group_id":"", "sender_id":"friend"})
        receipt = await self.store.record_rule_message(self.agent, source, now=self.now)
        state = await self.state()
        self.assertEqual(state.version, 1)
        self.assertEqual(state.needs.social_need, 49.5)
        self.assertEqual(state.needs.energy, 40)
        self.assertEqual(state.needs.satiety, 70)
        self.assertEqual(await self.store.record_rule_message(self.agent, source, now=self.now), receipt)
        self.assertEqual(await self.state(), state)

    async def test_already_expired_feedback_does_not_create_waiting_job(self):
        self.now += timedelta(hours=2)
        source = await self.store.report_activity_feedback(self.agent, "play-one", ActivityFeedback(
            checkpoint_id="old-event", task="failure", summary="两小时前的反馈", evaluation_mode="deferred"),
            now=self.now, observed_at=TIME)
        self.assertEqual(await self.state(), self.initial)
        self.assertEqual(await self.store.pending_appraisals(self.agent), ())
        self.assertIsNone(await self.store.get_appraisal(self.agent, source.event_id))
        receipt = await self.store.get_event(evaluation_id(self.agent, source.event_id))
        self.assertEqual(receipt.payload["layer"], "expired")

    async def test_legacy_feedback_payload_stays_byte_compatible(self):
        feedback = ActivityFeedback(checkpoint_id="old", outcome="interesting", summary="旧内容")
        event = build_feedback_event(self.running, feedback, at=self.now)
        self.assertEqual(event.payload["feedback"], dict(checkpoint_id="old", outcome="interesting", summary="旧内容",
                                                       stage=None, content_ref=None))
        await self.store.report_activity_feedback(self.agent, "play-one", feedback, now=self.now)
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.store.report_activity_feedback(self.agent, "play-one", feedback, now=self.now), event)

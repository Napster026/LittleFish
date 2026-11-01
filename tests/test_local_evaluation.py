"""本地规则、阶段反馈、去重与回滚；由开发者手动运行。"""

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.feedback import ActivityFeedback, build_feedback_event
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ActivityRecord, ActivityStatus, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.service import ActivityService
from persona_agent.cognition.appraisal import evaluate_chat
from persona_agent.cognition.evaluation import EvaluationSettings
from persona_agent.application.evaluation import evaluation_id
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.events import AgentEvent
from persona_agent.core.persistence import CorePersistence
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persistence.errors import EventConflictError
from persona_agent.persona.emotion import EmotionDynamics, EmotionState, VAD
from persona_agent.persona.needs import NeedState


TIME = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


class ChatRuleTests(unittest.TestCase):
    def test_directed_clear_phrases_and_conservative_ambiguity(self):
        settings = EvaluationSettings()
        self.assertEqual(evaluate_chat("谢谢你😊", directed=True, settings=settings).rule_id, "thanks")
        self.assertEqual(evaluate_chat("谢谢你", directed=False, settings=settings).reason, "not_directed")
        for text in ("他说你是笨蛋", "真是谢谢你", "并不是讨厌你", "电影很好", "谢谢你，但你真没用"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_chat(text, directed=True, settings=settings).impulse)
        insult = evaluate_chat("你真没用", directed=True, settings=settings)
        self.assertLess(insult.impulse.valence, 0)

    def test_caps_strict_config_and_disabled(self):
        result = evaluate_chat("你真棒" * 50, directed=True, settings=EvaluationSettings(max_axis_delta=.01))
        self.assertEqual(result.impulse.valence, .01)
        self.assertIsNone(evaluate_chat("你真棒", directed=True, settings=EvaluationSettings(enabled=False)).impulse)
        with self.assertRaises(ValidationError): EvaluationSettings(max_axis_delta=True)
        with self.assertRaises(ValidationError): EvaluationSettings(cooldown_seconds=.00001)
        with self.assertRaises(ValidationError): ActivityFeedback(
            checkpoint_id="one", outcome="interesting", summary="内容", status="completed",
        )


class LocalEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "rules.sqlite3"
        self.store = PersonaStore(SQLiteStorage(self.path), evaluation=EvaluationSettings())
        await self.store.open()
        self.now = TIME
        zero = VAD(valence=0, arousal=0, dominance=0)
        self.agent = "persona-001"
        self.initial = AgentState(agent_id=self.agent, updated_at=TIME,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
            emotion=EmotionState(current=zero, mood=zero, baseline=zero, updated_at=TIME,
                dynamics=EmotionDynamics(emotion_half_life_seconds=300, mood_half_life_seconds=7200, mood_impulse_ratio=.1)))
        await self.store.create_state(self.initial)
        self.definition = ActivityDefinition(definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10})
        instance = create_activity_instance(self.definition, activity_id="activity-one", agent_id=self.agent,
                                           mode=ExecutionMode.SIMULATED, created_at=TIME)
        await self.store.create_activity(self.definition, instance)
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.manager = ActivityManager(registry)
        self.service = ActivityService(self.agent, self.store, self.manager, clock=lambda: self.now)
        self.running = await self.service.start_activity("activity-one")

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    def source(self, identifier, *, group="", text="谢谢你"):
        return AgentEvent(event_id=identifier, event_type="SOCIAL.message_received", timestamp=self.now,
                          source="astrbot.aiocqhttp", provenance="observed",
                          scope_id=f"LittleFish:{group or 'friend'}", payload={"text": text, "group_id": group, "sender_id": "friend"})

    async def state(self):
        return await self.store.get_state(self.agent)

    async def test_chat_receipts_cooldown_across_bucket_and_restart(self):
        self.now += timedelta(seconds=29)
        first = self.source("one")
        receipt = await self.store.record_rule_message(self.agent, first, now=self.now)
        state = await self.state()
        self.assertEqual(state.emotion.current.valence, .03)
        self.assertEqual(await self.store.record_rule_message(self.agent, first, now=self.now), receipt)
        self.assertEqual(await self.state(), state)
        await self.store.close()
        self.now += timedelta(seconds=2)
        await self.store.open()
        suppressed = await self.store.record_rule_message(self.agent, self.source("two"), now=self.now)
        self.assertEqual(suppressed.payload["evaluation"]["reason"], "repeat_cooldown")
        self.assertEqual(await self.state(), state)
        self.now += timedelta(seconds=30)
        fresh = await self.store.record_rule_message(self.agent, self.source("three"), now=self.now)
        self.assertIsNotNone(fresh.payload["evaluation"]["impulse"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)

    async def test_plain_directed_message_relieves_social_need_without_emotion(self):
        before = await self.state()
        receipt = await self.store.record_rule_message(self.agent, self.source("plain", text="今天天气不错"),
                                                       now=self.now)
        self.assertEqual(receipt.payload["evaluation"]["reason"], "no_clear_rule")
        self.assertEqual(receipt.payload["evaluation"]["needs_reason"], "directed_engagement")
        state = await self.state()
        self.assertAlmostEqual(state.needs.social_need, before.needs.social_need - .3)
        self.assertEqual(state.emotion, before.emotion)
        self.assertEqual(state.needs.boredom, before.needs.boredom)
        self.assertEqual(state.needs.energy, before.needs.energy)

    async def test_disabled_master_gate_preserves_all_state_and_does_not_consume_cooldown(self):
        self.store._evaluation = EvaluationSettings(enabled=False)
        before = await self.state()
        cases = (("plain-off", "今天天气不错", "", False), ("positive-off", "谢谢你", "", False),
                 ("negative-off", "你真没用", "", False), ("empty-off", "", "", False),
                 ("group-off", "今天天气不错", "group", True))
        for identifier, text, group, directed in cases:
            with self.subTest(identifier=identifier):
                receipt = await self.store.record_rule_message(self.agent,
                    self.source(identifier, text=text, group=group), now=self.now, directed=directed)
                result = receipt.payload["evaluation"]
                self.assertEqual(result["reason"], "disabled")
                self.assertIsNone(result["impulse"])
                self.assertIsNone(result["needs_delta"])
                self.assertEqual(await self.state(), before)
        self.store._evaluation = EvaluationSettings()
        await self.store.record_rule_message(self.agent, self.source("enabled-again", text="今天天气不错"), now=self.now)
        state = await self.state()
        self.assertAlmostEqual(state.needs.social_need, before.needs.social_need-.3)
        self.assertEqual(state.version, before.version+1)
        self.assertEqual(state.emotion, before.emotion)

    async def test_group_target_gate_and_neutral_receipt(self):
        ignored = await self.store.record_rule_message(self.agent, self.source("group-one", group="group"), now=self.now)
        self.assertEqual(ignored.payload["evaluation"]["reason"], "not_directed")
        self.assertEqual(await self.state(), self.initial)
        accepted = await self.store.record_rule_message(self.agent, self.source("group-two", group="group"),
                                                         now=self.now, directed=True)
        self.assertEqual(accepted.payload["evaluation"]["rule_id"], "thanks")

    async def test_stage_content_updates_without_terminal_status_reward_or_tick_reset(self):
        before = self.running
        feedback = ActivityFeedback(checkpoint_id="item-1", outcome="interesting", summary="感兴趣的内容摘要", content_ref="item:1")
        first = await self.store.report_activity_feedback(self.agent, "activity-one", feedback, now=self.now)
        role = await self.state()
        self.assertEqual(role.emotion.current.valence, .06)
        self.assertEqual(role.needs.energy, self.initial.needs.energy)
        self.assertEqual(role.needs.satiety, self.initial.needs.satiety)
        self.assertEqual(role.needs.social_need, self.initial.needs.social_need)
        self.assertEqual(role.needs.boredom, self.initial.needs.boredom-2)
        self.assertEqual(await self.store.get_activity(self.agent, "activity-one"), before)
        self.now += timedelta(seconds=1)
        second = ActivityFeedback(checkpoint_id="item-2", outcome="neutral", summary="下一段内容")
        await self.store.report_activity_feedback(self.agent, "activity-one", second, now=self.now)
        latest = await self.store.latest_activity_feedback(self.agent, "activity-one")
        self.assertEqual(latest.payload["feedback"]["summary"], "下一段内容")
        self.assertEqual(await self.state(), role)
        await self.store.close()
        await self.store.open()
        self.assertEqual((await self.store.latest_activity_feedback(self.agent, "activity-one")).payload["feedback"]["checkpoint_id"], "item-2")
        self.assertEqual(await self.store.report_activity_feedback(self.agent, "activity-one", feedback, now=self.now), first)
        self.assertEqual(await self.state(), role)
        self.now += timedelta(seconds=4)
        from persona_agent.activity.ticks import build_activity_tick
        progressed = await self.service.handle_event(build_activity_tick(before).event)
        self.assertEqual(progressed.instance.progress, .5)
        self.assertEqual(progressed.instance.status, ActivityStatus.RUNNING)
        self.assertEqual((await self.state()).needs.energy, 40)

    async def test_executor_observe_feedback_and_whole_completion_are_separate(self):
        self.now += timedelta(seconds=1)
        context = ActivityContext(definition=self.definition, instance=self.running.instance, now=self.now)
        feedback = ActivityFeedback(checkpoint_id="checkpoint", outcome="failure", summary="子任务未成功，继续活动")
        event = build_feedback_event(self.running, feedback, at=self.now)
        result = ActivityExecutionResult(instance=self.running.instance, events=(event,))
        await self.store.commit_activity_result("observe", context, result)
        self.assertEqual((await self.store.get_activity(self.agent, "activity-one")).instance.status, ActivityStatus.RUNNING)
        self.assertEqual((await self.state()).needs.energy, 40)
        self.now = TIME + timedelta(seconds=10)
        terminal_context = ActivityContext(definition=self.definition, instance=self.running.instance, now=self.now)
        terminal = await self.manager.step(terminal_context)
        terminal_feedback = build_feedback_event(ActivityRecord(definition=self.definition, instance=terminal.instance),
            ActivityFeedback(checkpoint_id="bad-terminal", outcome="interesting", summary="不能把阶段反馈当作整项完成"), at=self.now)
        with self.assertRaises(ValueError):
            await self.store.commit_activity_result("step", terminal_context, ActivityExecutionResult(
                instance=terminal.instance, events=terminal.events+(terminal_feedback,)))
        self.assertEqual((await self.state()).needs.energy, 40)
        record = await self.service.pause_activity("activity-one")
        self.assertEqual(record.instance.status, ActivityStatus.COMPLETED)
        state = await self.state()
        self.assertEqual(state.needs.energy, 50)
        replayed = await self.store.report_activity_feedback(self.agent, "activity-one", feedback, now=self.now)
        self.assertEqual(replayed, event)
        self.assertEqual(await self.state(), state)
        with self.assertRaises(ValueError):
            await self.store.report_activity_feedback(self.agent, "activity-one", ActivityFeedback(
                checkpoint_id="late", outcome="interesting", summary="终态后的新反馈"), now=self.now)

    async def test_changed_checkpoint_conflicts_and_feedback_transaction_rolls_back(self):
        feedback = ActivityFeedback(checkpoint_id="same", outcome="interesting", summary="原内容")
        await self.store.report_activity_feedback(self.agent, "activity-one", feedback, now=self.now)
        with self.assertRaises(EventConflictError):
            await self.store.report_activity_feedback(self.agent, "activity-one", ActivityFeedback(
                checkpoint_id="same", outcome="interesting", summary="不同内容"), now=self.now)
        before = await self.state()
        next_feedback = ActivityFeedback(checkpoint_id="rollback", outcome="success", summary="事务测试")
        event = build_feedback_event(self.running, next_feedback, at=self.now)
        with patch.object(CorePersistence, "create_schedule", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.store.report_activity_feedback(self.agent, "activity-one", next_feedback, now=self.now)
        self.assertEqual(await self.state(), before)
        self.assertIsNone(await self.store.get_event(event.event_id))
        self.assertIsNone(await self.store.get_event(evaluation_id(self.agent, event.event_id)))
        self.assertEqual(await self.store.get_activity(self.agent, "activity-one"), self.running)

    async def test_whole_failure_on_exhausted_tick_has_one_emotion_effect_and_no_reward(self):
        from persona_agent.activity.ticks import build_activity_tick
        tick = build_activity_tick(self.running)
        plan = await self.store.get_schedule(self.agent, tick.schedule_id)
        for attempt in range(tick.max_attempts):
            self.now = plan.next_attempt_at
            plan = await self.store.record_schedule_delivery(self.agent, tick.schedule_id,
                expected_attempts=attempt, attempted_at=self.now, succeeded=False, error="执行失败")
        record = await self.store.get_activity(self.agent, "activity-one")
        self.assertEqual(record.instance.status, ActivityStatus.FAILED)
        state = await self.state()
        self.assertEqual(state.needs.energy, 40)
        self.assertEqual(state.emotion.current.valence, -.06)
        receipt = await self.store.latest_evaluation(self.agent)
        self.assertEqual(receipt.payload["evaluation"]["rule_id"], "activity.activity_failed")
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.state(), state)
        self.assertEqual(await self.store.latest_evaluation(self.agent), receipt)

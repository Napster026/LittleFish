"""自主活动的选择、事务与生命周期回归，由开发者手动运行。"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from activity.executors.simulated import SimulatedActivityExecutor
from activity.manager import ActivityManager
from activity.models import ActivityDefinition, ActivityStatus, ExecutionMode, create_activity_instance
from activity.registry import ExecutorRegistry
from activity.selection import ActivitySelector, SelectionPolicy
from activity.service import ActivityService
from activity.ticks import build_activity_tick
from application.autonomy import AutonomousActivityService, selection_id
from application.needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler
from application.persistence import PersonaStore
from core.persistence import CorePersistence
from core.arbiter import ArbitrationPolicy, BehaviorArbiter, BehaviorKind
from core.events import AgentEvent
from core.router import EventRouter
from core.runtime import AgentRuntime
from core.scheduler import TimeScheduler
from core.state import AgentState
from infrastructure.storage import SQLiteStorage
from persona.needs import NeedRates, NeedState


def definition(identifier="rest", **effect):
    return ActivityDefinition(
        definition_id=identifier, name=identifier, supported_modes=["simulated"],
        estimated_duration_seconds=10, completion_needs_delta=effect or {"energy": 50},
    )


class SelectionTests(unittest.TestCase):
    def test_urgency_side_effects_ties_and_idle(self):
        needs = NeedState(energy=20, satiety=100, boredom=0, social_need=0)
        good = definition("a", energy=50)
        tie = definition("b", energy=50)
        harmful = definition("harmful", energy=50, satiety=-100)
        selector = ActivitySelector((tie, harmful, good))
        self.assertEqual(selector.choose(needs).definition, good)
        self.assertLess(dict(selector.choose(needs).scores)["harmful"], 0)
        self.assertIsNone(selector.choose(NeedState(
            energy=100, satiety=100, boredom=0, social_need=0,
        )).definition)
        # 到边界的实际恢复量计分；需求方向应正确处理。
        social = definition("social", social_need=-200)
        result = ActivitySelector((social, good)).choose(NeedState(
            energy=100, satiety=100, boredom=0, social_need=80,
        ))
        self.assertEqual(result.definition, social)
        self.assertAlmostEqual(dict(result.scores)["social"], 0.64)

    def test_ineligible_definitions_and_policy_validation(self):
        valid = definition()
        real = ActivityDefinition(**{**valid.model_dump(), "supported_modes": ["real"]})
        unknown = ActivityDefinition(**{**valid.model_dump(), "estimated_duration_seconds": None})
        needs = NeedState(energy=0, satiety=0, boredom=100, social_need=100)
        self.assertIsNone(ActivitySelector((real,)).choose(needs).definition)
        self.assertIsNone(ActivitySelector((unknown,)).choose(needs).definition)
        self.assertIsNone(ActivitySelector((valid,), policy=SelectionPolicy(
            energy=0, satiety=0, boredom=0, social_need=0,
        )).choose(needs).definition)
        with self.assertRaises(ValueError):
            ActivitySelector((valid, valid))
        for value in (True, "1", float("nan"), -1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SelectionPolicy(energy=value)


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.store = PersonaStore(SQLiteStorage(Path(self.directory.name) / "agent.sqlite3"))
        await self.store.open()
        self.now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        self.agent = "persona-001"
        await self.store.create_state(AgentState(
            agent_id=self.agent, updated_at=self.now,
            needs=NeedState(energy=20, satiety=100, boredom=0, social_need=0),
        ))
        self.rest = definition()
        self.selector = ActivitySelector((self.rest,))
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.activities = ActivityService(
            self.agent, self.store, ActivityManager(registry), clock=lambda: self.now,
        )
        router = EventRouter()
        router.register(NEEDS_TICK_EVENT_TYPE, NeedTickHandler(clock=lambda: self.now))
        self.runtime = AgentRuntime(self.agent, self.store, router, clock=lambda: self.now)
        await self.runtime.start()
        self.service = self.make_service()
        self.scheduler = TimeScheduler(
            self.agent, self.store, self.service.handle_event, clock=lambda: self.now,
        )

    def make_service(self):
        return AutonomousActivityService(
            self.agent, self.store, self.selector, self.activities, self.runtime,
            clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.scheduler.stop()
        await self.runtime.stop()
        await self.store.close()
        self.directory.cleanup()

    async def test_need_tick_start_completion_and_replay(self):
        plan = await self.store.start_needs_clock(
            self.agent, rates=NeedRates(energy=-4, satiety=0, boredom=0, social_need=0), now=self.now,
        )
        await self.scheduler.run_due()
        identifier = selection_id(self.agent, plan.schedule.event.event_id)
        started = await self.store.get_activity(self.agent, identifier)
        self.assertEqual(started.instance.status, ActivityStatus.RUNNING)
        await self.service.handle_event(plan.schedule.event)
        self.assertEqual(await self.store.get_activity(self.agent, identifier), started)
        self.now += timedelta(seconds=10)
        await self.scheduler.run_due()
        completed = await self.store.get_activity(self.agent, identifier)
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)
        state = await self.store.get_state(self.agent)
        self.assertEqual(state.needs.energy, 70)
        # 原需求事件重投不重新选择，即使原活动已经结束。
        await self.service.handle_event(plan.schedule.event)
        await self.service.handle_event(build_activity_tick(started).event)
        self.assertEqual(await self.store.get_state(self.agent), state)
        self.assertIsNone(await self.service.consider("idle"))

    async def test_pending_survives_restart_and_start_failure(self):
        with patch.object(self.activities, "start_activity", AsyncMock(side_effect=RuntimeError("start failed"))):
            with self.assertRaises(RuntimeError):
                await self.service.consider("choose")
        pending = await self.store.get_activity(self.agent, selection_id(self.agent, "choose"))
        self.assertEqual(pending.instance.status, ActivityStatus.PENDING)
        await self.store.close()
        await self.store.open()
        recovered = await self.make_service().recover()
        self.assertEqual(recovered.instance.activity_id, pending.instance.activity_id)
        self.assertEqual(recovered.instance.status, ActivityStatus.RUNNING)
        self.assertEqual(await self.service.consider("choose"), recovered)
        self.assertIsNone(await self.service.consider("busy"))
        # 暂停也占用角色，不自动恢复或切换活动。
        await self.activities.pause_activity(recovered.instance.activity_id)
        self.assertIsNone(await self.service.consider("paused"))

    async def test_manual_pending_not_started_and_busy_receipt_is_final(self):
        manual = create_activity_instance(
            self.rest, activity_id="manual", agent_id=self.agent,
            mode=ExecutionMode.SIMULATED, created_at=self.now,
        )
        await self.store.create_activity(self.rest, manual)
        self.assertIsNone(await self.service.recover())
        self.assertIsNone(await self.service.consider("blocked"))
        await self.activities.stop_activity("manual")
        self.assertIsNone(await self.service.consider("blocked"))
        selected = await self.service.consider("new")
        self.assertEqual(selected.instance.status, ActivityStatus.RUNNING)

    async def test_receipt_failure_rolls_back_instance(self):
        with patch.object(CorePersistence, "record_event", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.service.consider("rollback")
        identifier = selection_id(self.agent, "rollback")
        self.assertIsNone(await self.store.get_activity(self.agent, identifier))
        self.assertIsNone(await self.store.get_event(identifier))
        self.assertEqual((await self.service.consider("rollback")).instance.activity_id, identifier)

    async def test_concurrent_decisions_reserve_one_instance(self):
        # 绕过服务内存锁，验证存储事务本身的检查与创建边界。
        first, second = await asyncio.gather(*(
            self.store.reserve_next_activity(
                self.agent, self.selector, decision_key=key, now=self.now,
            ) for key in ("first", "second")
        ))
        self.assertEqual(first.instance.activity_id, second.instance.activity_id)
        self.assertEqual(first.instance.status, ActivityStatus.PENDING)
        started = await self.service.recover()
        self.assertEqual(started.instance.activity_id, first.instance.activity_id)

    async def test_disabled_top_choice_falls_back_and_records_arbitration(self):
        alternative = definition("short_rest", energy=10)
        registry = ExecutorRegistry()
        registry.register("short_rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        activities = ActivityService(self.agent, self.store, ActivityManager(registry), clock=lambda: self.now)
        service = AutonomousActivityService(
            self.agent, self.store, ActivitySelector((self.rest, alternative)), activities, self.runtime,
            clock=lambda: self.now,
            arbiter=BehaviorArbiter(ArbitrationPolicy(disabled_definition_ids={"rest"})),
        )
        started = await service.consider("fallback")
        self.assertEqual(started.instance.definition_id, "short_rest")
        self.assertEqual(service.last_decision.winner.kind, BehaviorKind.START_ACTIVITY)
        receipt = await self.store.get_event(selection_id(self.agent, "fallback"))
        self.assertEqual(receipt.payload["arbitration"]["winner"]["definition_id"], "short_rest")
        self.assertEqual(receipt.payload["arbitration_policy"]["disabled_definition_ids"], ["rest"])
        self.assertIsNone(await service.consider("continue"))
        self.assertEqual(service.last_decision.winner.kind, BehaviorKind.CONTINUE_ACTIVITY)

    async def test_pending_receipt_rechecks_current_policy_and_accepts_legacy_receipt(self):
        # 构造上一版选择回执，模拟升级前已保存但未启动的活动。
        identifier = selection_id(self.agent, "legacy")
        pending = create_activity_instance(
            self.rest, activity_id=identifier, agent_id=self.agent,
            mode=ExecutionMode.SIMULATED, created_at=self.now,
        )
        await self.store.create_activity(self.rest, pending)
        await self.store.record_event(AgentEvent(
            event_id=identifier, event_type="INTERNAL.activity_selected", source="application.autonomy",
            timestamp=self.now, provenance="inferred", activity_id=identifier,
            payload={"agent_id": self.agent, "decision_key": "legacy", "reason": "selected",
                     "scores": {"rest": 0.55}, "state_version": 0},
        ))
        blocked = AutonomousActivityService(
            self.agent, self.store, self.selector, self.activities, self.runtime,
            clock=lambda: self.now,
            arbiter=BehaviorArbiter(ArbitrationPolicy(disabled_definition_ids={"rest"})),
        )
        self.assertIsNone(await blocked.consider("legacy"))
        self.assertEqual(blocked.last_decision.winner.kind, BehaviorKind.IDLE)
        self.assertEqual((await self.store.get_activity(self.agent, identifier)).instance, pending)
        started = await self.service.consider("legacy")
        self.assertEqual(started.instance.activity_id, identifier)
        self.assertEqual(started.instance.status, ActivityStatus.RUNNING)
        # 升级兼容不会重写原回执。
        self.assertNotIn("arbitration", (await self.store.get_event(identifier)).payload)

"""完成需求效果、原子提交与旧活动补结算测试，由开发者手动运行。"""

import asyncio
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import (
    ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus,
    ExecutionMode, create_activity_instance,
)
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.rewards import (
    ActivityNotCompletedError, InvalidCompletionEventError, build_completion_proposal,
)
from persona_agent.core.events import AgentEvent
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.settlement import SettlementTimeError, StateChangeProposal
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.application.persistence import PersonaStore
from persona_agent.persistence.errors import (
    ProposalConflictError,
)
from persona_agent.persona.needs import NeedState


class ActivityRewardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "agent.sqlite3"
        self.storage = PersonaStore(SQLiteStorage(self.path))
        await self.storage.open()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.state = AgentState(
            agent_id="persona-001", updated_at=self.time,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
        )
        await self.storage.create_state(self.state)
        self.definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10},
        )
        self.initial = create_activity_instance(
            self.definition, activity_id="activity-001", agent_id=self.state.agent_id,
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.manager = ActivityManager(registry)

    async def asyncTearDown(self):
        await self.storage.close()
        self.directory.cleanup()

    def context(self, instance, seconds):
        return ActivityContext(
            definition=self.definition, instance=instance,
            now=self.time + timedelta(seconds=seconds),
        )

    async def ready_to_complete(self):
        await self.storage.create_activity(self.definition, self.initial)
        context = self.context(self.initial, 0)
        started = await self.manager.start(context)
        running = await self.storage.commit_activity_result("start", context, started)
        final_context = self.context(running.instance, 10)
        final_result = await self.manager.step(final_context)
        return running, final_context, final_result

    async def complete(self):
        _, context, result = await self.ready_to_complete()
        record = await self.storage.commit_activity_result("step", context, result)
        return record, context, result

    async def legacy_unpaid(self):
        record, context, result = await self.complete()
        # 临时测试夹具：模拟上一个版本已保存完成活动、尚未发放需求效果的数据。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "DELETE FROM state_settlements WHERE agent_id = ? AND event_id = ?",
                (self.state.agent_id, result.events[0].event_id),
            )
            connection.execute(
                "UPDATE agent_states SET version = ?, state_json = ? WHERE agent_id = ?",
                (self.state.version, self.state.model_dump_json(), self.state.agent_id),
            )
            connection.commit()
        return record, context, result

    async def test_proposal_uses_fixed_definition_and_stable_identity(self):
        record, _, result = await self.complete()
        first = build_completion_proposal(record, result.events[0], self.state)
        second = build_completion_proposal(record, result.events[0], self.state)
        self.assertEqual(first, second)
        self.assertEqual(first.needs_delta, self.definition.completion_needs_delta)
        self.assertEqual(first.expected_version, 0)

    async def test_invalid_completion_metadata_is_rejected(self):
        record, _, result = await self.complete()
        for key, value in (
            ("agent_id", "other"), ("definition_id", "other"), ("mode", "real"),
            ("status", "running"), ("revision", True), ("progress", 0.5),
        ):
            with self.subTest(key=key):
                data = result.events[0].model_dump()
                data["payload"][key] = value
                with self.assertRaises(InvalidCompletionEventError):
                    build_completion_proposal(record, AgentEvent(**data), self.state)
        event = AgentEvent(**{**result.events[0].model_dump(), "timestamp": self.time + timedelta(seconds=20)})
        with self.assertRaises(InvalidCompletionEventError):
            build_completion_proposal(record, event, self.state)

    async def test_feedback_cannot_override_effect_or_provenance(self):
        record, _, result = await self.complete()
        for updates in ({"needs_delta": {"energy": 20}}, {"needs_delta": {}}, {"needs_delta": None}):
            data = result.events[0].model_dump()
            data["payload"].update(updates)
            with self.assertRaises(InvalidCompletionEventError):
                build_completion_proposal(record, AgentEvent(**data), self.state)
        event = AgentEvent(**{**result.events[0].model_dump(), "provenance": "inferred"})
        with self.assertRaises(InvalidCompletionEventError):
            build_completion_proposal(record, event, self.state)

    async def test_no_configured_effect_does_not_change_role_version(self):
        self.definition = ActivityDefinition(**{
            **self.definition.model_dump(), "completion_needs_delta": None,
        })
        record, _, result = await self.complete()
        self.assertIsNone(build_completion_proposal(record, result.events[0], self.state))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        retried = await self.storage.settle_activity_completion(
            self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
        )
        self.assertEqual(retried, self.state)

    async def test_completion_commits_activity_event_role_and_proposal(self):
        record, _, result = await self.complete()
        role = await self.storage.get_state(self.state.agent_id)
        self.assertEqual(record.instance.status, ActivityStatus.COMPLETED)
        self.assertEqual(role.needs.energy, 50)
        self.assertEqual(role.version, 1)
        self.assertEqual(await self.storage.get_event(result.events[0].event_id), result.events[0])
        self.assertIsNotNone(await self.storage.get_committed_proposal(self.state.agent_id, result.events[0].event_id))

    async def test_retry_does_not_reissue_effect_after_later_role_update(self):
        record, context, result = await self.complete()
        event = AgentEvent(
            event_id="later", event_type="INTERNAL.needs_update", timestamp=self.time + timedelta(seconds=20),
            source="rules", provenance="observed", payload={},
        )
        await self.storage.record_event(event)
        latest = await self.storage.commit_proposal(StateChangeProposal(
            proposal_id="later", event_id=event.event_id, agent_id=self.state.agent_id,
            expected_version=1, needs_delta={"energy": 3},
        ), settled_at=event.timestamp)
        self.assertEqual(await self.storage.commit_activity_result("step", context, result), record)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), latest)
        self.assertEqual(latest.needs.energy, 53)
        self.assertEqual(latest.version, 2)

    async def test_runtime_replay_skips_already_settled_completion_rule(self):
        _, _, result = await self.complete()
        router = EventRouter()
        handler = AsyncMock(side_effect=AssertionError("completion rule must not repeat"))
        router.register("ACTIVITY.completed", handler)
        async with AgentRuntime(
            self.state.agent_id, self.storage, router,
            clock=lambda: self.time + timedelta(seconds=20),
        ) as runtime:
            role = await runtime.submit(result.events[0])
        handler.assert_not_called()
        self.assertEqual(role.needs.energy, 50)
        self.assertEqual(role.version, 1)

    async def test_unfinished_or_cancelled_activity_cannot_receive_completion_effect(self):
        running, _, _ = await self.ready_to_complete()
        with self.assertRaises(ActivityNotCompletedError):
            await self.storage.settle_activity_completion(
                self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
            )
        context = self.context(running.instance, 2)
        cancelled = await self.manager.stop(context)
        await self.storage.commit_activity_result("stop", context, cancelled)
        with self.assertRaises(ActivityNotCompletedError):
            await self.storage.settle_activity_completion(
                self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
            )
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_multiple_completion_events_roll_back_entire_commit(self):
        running, context, result = await self.ready_to_complete()
        invalid = ActivityExecutionResult(instance=result.instance, events=result.events + result.events)
        with self.assertRaises(InvalidCompletionEventError):
            await self.storage.commit_activity_result("step", context, invalid)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_tampered_effect_rolls_back_activity_and_event(self):
        running, context, result = await self.ready_to_complete()
        data = result.events[0].model_dump()
        data["payload"]["needs_delta"] = {"energy": 20}
        invalid = ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
        with self.assertRaises(InvalidCompletionEventError):
            await self.storage.commit_activity_result("step", context, invalid)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))

    async def test_role_settlement_failure_rolls_back_all_activity_writes(self):
        running, context, result = await self.ready_to_complete()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                CREATE TRIGGER fail_reward BEFORE INSERT ON state_settlements
                BEGIN SELECT RAISE(ABORT, 'reward failure'); END;
            """)
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.storage.commit_activity_result("step", context, result)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER fail_reward")
            connection.commit()
        await self.storage.commit_activity_result("step", context, result)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).needs.energy, 50)

    async def test_late_effect_requires_valid_explicit_settlement_time(self):
        running, context, result = await self.ready_to_complete()
        event = AgentEvent(
            event_id="newer", event_type="INTERNAL.needs_update", timestamp=self.time + timedelta(seconds=20),
            source="rules", provenance="observed", payload={},
        )
        await self.storage.record_event(event)
        await self.storage.commit_proposal(StateChangeProposal(
            proposal_id="newer", event_id=event.event_id, agent_id=self.state.agent_id,
            expected_version=0, needs_delta={"energy": 1},
        ), settled_at=event.timestamp)
        with self.assertRaises(SettlementTimeError):
            await self.storage.commit_activity_result("step", context, result)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)
        await self.storage.commit_activity_result(
            "step", context, result, settled_at=self.time + timedelta(seconds=21),
        )
        role = await self.storage.get_state(self.state.agent_id)
        self.assertEqual(role.needs.energy, 51)
        self.assertEqual(role.version, 2)

    async def test_conflicting_prior_rule_is_not_silently_rewarded_again(self):
        running, context, result = await self.ready_to_complete()
        event = result.events[0]
        await self.storage.record_event(event)
        prior = await self.storage.commit_proposal(StateChangeProposal(
            proposal_id="other-rule", event_id=event.event_id, agent_id=self.state.agent_id,
            expected_version=0, needs_delta={"energy": 20},
        ), settled_at=context.now)
        with self.assertRaises(ProposalConflictError):
            await self.storage.commit_activity_result("step", context, result)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), prior)

    async def test_two_connections_backfill_effect_only_once(self):
        await self.legacy_unpaid()
        async with PersonaStore(SQLiteStorage(self.path)) as other:
            results = await asyncio.gather(
                self.storage.settle_activity_completion(
                    self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
                ),
                other.settle_activity_completion(
                    self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
                ),
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].needs.energy, 50)
        self.assertEqual(results[0].version, 1)

    async def test_legacy_completion_can_be_backfilled_after_reopen(self):
        await self.legacy_unpaid()
        await self.storage.close()
        await self.storage.open()
        first = await self.storage.settle_activity_completion(
            self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
        )
        second = await self.storage.settle_activity_completion(
            self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=21),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.needs.energy, 50)
        self.assertEqual(first.version, 1)

    async def test_completed_label_without_commit_evidence_is_rejected(self):
        running, _, result = await self.ready_to_complete()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE activity_instances SET revision = ?, instance_json = ? WHERE activity_id = ?",
                (result.instance.revision, result.instance.model_dump_json(), self.initial.activity_id),
            )
            connection.commit()
        with self.assertRaises(InvalidCompletionEventError):
            await self.storage.settle_activity_completion(
                self.state.agent_id, self.initial.activity_id, settled_at=self.time + timedelta(seconds=20),
            )
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_missing_completion_event_rolls_back(self):
        running, context, result = await self.ready_to_complete()
        data = result.events[0].model_dump()
        data["event_type"] = "ACTIVITY.progressed"
        invalid = ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
        with self.assertRaises(InvalidCompletionEventError):
            await self.storage.commit_activity_result("step", context, invalid)
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, self.initial.activity_id), running)

    async def test_real_and_hybrid_effects_require_observed_completion(self):
        record, _, result = await self.complete()
        for mode in (ExecutionMode.REAL, ExecutionMode.HYBRID):
            definition = ActivityDefinition(**{**record.definition.model_dump(), "supported_modes": [mode]})
            instance = ActivityInstance(**{**record.instance.model_dump(), "mode": mode})
            real_record = ActivityRecord(definition=definition, instance=instance)
            data = result.events[0].model_dump()
            data["payload"]["mode"] = mode.value
            data["source"] = "activity.real"
            with self.assertRaises(InvalidCompletionEventError):
                build_completion_proposal(real_record, AgentEvent(**data), self.state)
            data["provenance"] = "observed"
            self.assertIsNotNone(build_completion_proposal(real_record, AgentEvent(**data), self.state))

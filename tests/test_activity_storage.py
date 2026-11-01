"""活动业务提交、原子反馈与结构迁移测试，由开发者手动运行。"""

from schema_fixtures import restore_v3_layout

import asyncio
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import (
    ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus,
    ExecutionMode, create_activity_instance,
)
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.core.events import AgentEvent
from persona_agent.core.settlement import StateChangeProposal
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.application.persistence import PersonaStore
from persona_agent.persistence.errors import (
    ActivityAlreadyExistsError,
    ActivityCommitConflictError,
    ActivityDefinitionConflictError,
    ActivityNotFoundError,
    ActivityVersionConflictError,
    EventConflictError,
    StateNotFoundError,
)
from persona_agent.persona.needs import NeedState


class ActivityStorageTests(unittest.IsolatedAsyncioTestCase):
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

    def context(self, instance=None, seconds=0, definition=None):
        return ActivityContext(
            definition=self.definition if definition is None else definition,
            instance=self.initial if instance is None else instance,
            now=self.time + timedelta(seconds=seconds),
        )

    async def read(self):
        return await self.storage.get_activity(self.state.agent_id, self.initial.activity_id)

    async def start(self):
        context = self.context()
        result = await self.manager.start(context)
        record = await self.storage.commit_activity_result("start", context, result)
        return context, result, record

    async def test_paused_activity_and_definition_survive_reopen(self):
        await self.storage.create_activity(self.definition, self.initial)
        _, _, running = await self.start()
        context = self.context(running.instance, 2)
        paused = await self.manager.pause(context)
        await self.storage.commit_activity_result("pause", context, paused)
        await self.storage.close()
        await self.storage.open()
        restored = await self.read()
        self.assertEqual(restored.definition, self.definition)
        self.assertEqual(restored.instance.status, ActivityStatus.PAUSED)
        self.assertAlmostEqual(restored.instance.progress, 0.2)
        resumed_context = self.context(restored.instance, 50, restored.definition)
        resumed = await self.manager.resume(resumed_context)
        saved = await self.storage.commit_activity_result("resume", resumed_context, resumed)
        self.assertEqual(saved.instance.revision, 3)
        self.assertAlmostEqual(saved.instance.progress, 0.2)

    async def test_missing_and_wrong_role_reads_are_isolated(self):
        self.assertIsNone(await self.read())
        context = self.context()
        result = await self.manager.start(context)
        with self.assertRaises(ActivityNotFoundError):
            await self.storage.commit_activity_result("start", context, result)
        await self.storage.create_activity(self.definition, self.initial)
        self.assertIsNone(await self.storage.get_activity("other-role", self.initial.activity_id))

    async def test_creation_cannot_overwrite_existing_activity(self):
        original = await self.storage.create_activity(self.definition, self.initial)
        with self.assertRaises(ActivityAlreadyExistsError):
            await self.storage.create_activity(self.definition, self.initial)
        self.assertEqual(await self.read(), original)

    async def test_missing_role_rejects_creation(self):
        instance = ActivityInstance(**{**self.initial.model_dump(), "agent_id": "missing"})
        with self.assertRaises(StateNotFoundError):
            await self.storage.create_activity(self.definition, instance)

    async def test_only_initial_pending_instance_can_be_created(self):
        for updates in (
            {"status": "running"}, {"revision": 1},
            {"updated_at": self.time + timedelta(seconds=1)},
        ):
            with self.subTest(updates=updates):
                instance = ActivityInstance(**{**self.initial.model_dump(), **updates})
                with self.assertRaises(ValueError):
                    await self.storage.create_activity(self.definition, instance)
        self.assertIsNone(await self.read())

    async def test_activity_feedback_and_role_reward_commit_together(self):
        await self.storage.create_activity(self.definition, self.initial)
        _, _, running = await self.start()
        context = self.context(running.instance, 10)
        result = await self.manager.step(context)
        completed = await self.storage.commit_activity_result("step", context, result)
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)
        self.assertEqual(await self.storage.get_event(result.events[0].event_id), result.events[0])
        role = await self.storage.get_state(self.state.agent_id)
        self.assertEqual(role.needs.energy, 50)
        self.assertEqual(role.version, 1)
        self.assertIsNotNone(await self.storage.get_committed_proposal(self.state.agent_id, result.events[0].event_id))

    async def test_changed_or_future_snapshot_is_rejected(self):
        await self.storage.create_activity(self.definition, self.initial)
        _, _, running = await self.start()
        for updates in ({"progress": 0.1}, {"revision": 10}):
            with self.subTest(updates=updates):
                forged = ActivityInstance(**{**running.instance.model_dump(), **updates})
                context = self.context(forged, 1)
                result = await self.manager.step(context)
                with self.assertRaises(ActivityVersionConflictError):
                    await self.storage.commit_activity_result("step", context, result)
        self.assertEqual(await self.read(), running)

    async def test_retry_returns_latest_activity_without_reverting(self):
        await self.storage.create_activity(self.definition, self.initial)
        original_context, original_result, running = await self.start()
        context = self.context(running.instance, 3)
        result = await self.manager.step(context)
        latest = await self.storage.commit_activity_result("step", context, result)
        retried = await self.storage.commit_activity_result("start", original_context, original_result)
        self.assertEqual(retried, latest)
        self.assertEqual(latest.instance.revision, 2)
        self.assertAlmostEqual(latest.instance.progress, 0.3)

    async def test_different_result_for_same_old_revision_is_rejected(self):
        await self.storage.create_activity(self.definition, self.initial)
        context, original, saved = await self.start()
        event = AgentEvent(**{**original.events[0].model_dump(), "source": "activity.changed"})
        different = ActivityExecutionResult(instance=original.instance, events=(event,))
        with self.assertRaises(ActivityCommitConflictError):
            await self.storage.commit_activity_result("start", context, different)
        self.assertEqual(await self.read(), saved)

    async def test_commit_record_failure_rolls_back_activity_and_events(self):
        await self.storage.create_activity(self.definition, self.initial)
        context = self.context()
        result = await self.manager.start(context)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                CREATE TRIGGER fail_activity_commit BEFORE INSERT ON activity_commits
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.storage.commit_activity_result("start", context, result)
        self.assertEqual((await self.read()).instance, self.initial)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER fail_activity_commit")
            connection.commit()
        self.assertEqual((await self.storage.commit_activity_result("start", context, result)).instance.revision, 1)

    async def test_feedback_conflict_rolls_back_activity(self):
        await self.storage.create_activity(self.definition, self.initial)
        context = self.context()
        result = await self.manager.start(context)
        conflicting = AgentEvent(**{**result.events[0].model_dump(), "source": "other"})
        await self.storage.record_event(conflicting)
        with self.assertRaises(EventConflictError):
            await self.storage.commit_activity_result("start", context, result)
        self.assertEqual((await self.read()).instance, self.initial)
        self.assertEqual(await self.storage.get_event(conflicting.event_id), conflicting)

    async def test_original_definition_snapshot_cannot_be_replaced(self):
        await self.storage.create_activity(self.definition, self.initial)
        changed = ActivityDefinition(**{**self.definition.model_dump(), "estimated_duration_seconds": 20})
        context = self.context(definition=changed)
        result = await self.manager.start(context)
        with self.assertRaises(ActivityDefinitionConflictError):
            await self.storage.commit_activity_result("start", context, result)
        self.assertEqual((await self.read()).definition, self.definition)

    async def test_noop_does_not_consume_revision_commit_slot(self):
        await self.storage.create_activity(self.definition, self.initial)
        _, _, running = await self.start()
        context = self.context(running.instance)
        unchanged = await self.manager.step(context)
        self.assertEqual(await self.storage.commit_activity_result("step", context, unchanged), running)
        later_context = self.context(running.instance, 2)
        later = await self.manager.step(later_context)
        self.assertEqual((await self.storage.commit_activity_result("step", later_context, later)).instance.revision, 2)

    async def test_two_connections_cannot_commit_different_results_from_same_revision(self):
        await self.storage.create_activity(self.definition, self.initial)
        first_context, second_context = self.context(), self.context(seconds=1)
        first = await self.manager.start(first_context)
        second = await self.manager.start(second_context)
        async with PersonaStore(SQLiteStorage(self.path)) as other:
            results = await asyncio.gather(
                self.storage.commit_activity_result("start", first_context, first),
                other.commit_activity_result("start", second_context, second),
                return_exceptions=True,
            )
        self.assertEqual(sum(isinstance(item, ActivityRecord) for item in results), 1)
        self.assertEqual(sum(isinstance(item, ActivityCommitConflictError) for item in results), 1)
        self.assertEqual((await self.read()).instance.revision, 1)

    async def test_v1_upgrade_preserves_role_events_and_settlements(self):
        event = AgentEvent(
            event_id="legacy-event", event_type="INTERNAL.needs_update", timestamp=self.time,
            source="rules", provenance="observed", payload={"reason": "legacy"},
        )
        proposal = StateChangeProposal(
            proposal_id="legacy-proposal", event_id=event.event_id, agent_id=self.state.agent_id,
            expected_version=0, needs_delta={"energy": 1},
        )
        await self.storage.record_event(event)
        expected_state = await self.storage.commit_proposal(proposal, settled_at=self.time)
        await self.storage.close()
        # 移除后续版本增加的表，保留已有数据，模拟真正的 v1 结构。
        with closing(sqlite3.connect(self.path)) as connection:
            restore_v3_layout(connection)
            connection.execute("DROP TABLE scheduled_events")
            connection.execute("DROP TABLE activity_commits")
            connection.execute("DROP TABLE activity_instances")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        await self.storage.open()
        self.assertEqual(await self.storage.get_state(self.state.agent_id), expected_state)
        self.assertEqual(await self.storage.get_event(event.event_id), event)
        self.assertEqual(await self.storage.get_committed_proposal(self.state.agent_id, event.event_id), proposal)
        await self.storage.create_activity(self.definition, self.initial)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 7)

    async def test_failed_migration_rolls_back_schema_and_version(self):
        await self.storage.close()
        with closing(sqlite3.connect(self.path)) as connection:
            restore_v3_layout(connection)
            connection.execute("DROP TABLE scheduled_events")
            connection.execute("DROP TABLE activity_commits")
            connection.execute("DROP TABLE activity_instances")
            connection.execute("PRAGMA user_version = 1")
            # 第二条建表语句失败，用来验证第一条建表也会回滚。
            connection.execute("CREATE VIEW activity_commits AS SELECT 1 AS value")
            connection.commit()
        with self.assertRaises(sqlite3.OperationalError):
            await self.storage.open()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'activity_instances'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT version FROM agent_states").fetchone()[0], 0)
            connection.execute("DROP VIEW activity_commits")
            connection.commit()
        await self.storage.open()
        await self.storage.create_activity(self.definition, self.initial)

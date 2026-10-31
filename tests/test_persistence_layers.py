"""持久化分层与适配器替换验证，由开发者手动运行。"""

import ast
import asyncio
import copy
import json
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3

from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ActivityStatus, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.service import ActivityService
from persona_agent.activity.ticks import build_activity_tick
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.events import AgentEvent
from persona_agent.core.persistence import CorePersistence
from persona_agent.core.settlement import StateChangeProposal
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persistence.errors import StorageNotOpenError
from persona_agent.persona.needs import NeedState


class MemorySession:
    """测试用端口实现，复制事务快照，无 SQL 或业务规则。"""

    def __init__(self, rows, backend):
        self.rows, self.backend = rows, backend
        self.active = True

    def check(self):
        if not self.active:
            raise StorageNotOpenError("会话已结束")

    async def fetch_one(self, table, *, where):
        rows = await self.fetch_all(table, where=where, limit=1)
        return rows[0] if rows else None

    async def fetch_all(self, table, *, where=None, at_most=None, after=None, order_by=(), limit=100):
        self.check()
        rows = [row for row in self.rows.get(table, []) if (
            all(row.get(k) == v for k, v in (where or {}).items())
            and all(row[k] <= v for k, v in (at_most or {}).items())
            and all(row[k] > v for k, v in (after or {}).items())
        )]
        if order_by:
            rows.sort(key=lambda row: tuple(row[key] for key in order_by))
        return tuple(copy.deepcopy(rows[:limit]))

    async def insert(self, table, values):
        self.check()
        if table == self.backend.fail_table:
            raise RuntimeError("injected insert failure")
        self.rows.setdefault(table, []).append(copy.deepcopy(values))

    async def update(self, table, values, *, where):
        self.check()
        count = 0
        for row in self.rows.get(table, []):
            if all(row.get(key) == value for key, value in where.items()):
                row.update(copy.deepcopy(values))
                count += 1
        return count


class MemoryUnitOfWork:
    def __init__(self):
        self.rows = {}
        self.opened = False
        self.fail_table = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.opened = True

    async def close(self):
        self.opened = False

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            if not self.opened:
                raise StorageNotOpenError("存储未打开")
            session = MemorySession(copy.deepcopy(self.rows), self)
            try:
                yield session
                self.rows = session.rows
            finally:
                session.active = False


class PersistenceLayersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "layers.sqlite3"
        self.backend = SQLiteStorage(self.path)
        await self.backend.open()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.now = self.time

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    def state(self):
        return AgentState(agent_id="persona-001", updated_at=self.time, needs=NeedState(
            energy=40, satiety=70, boredom=30, social_need=50,
        ))

    async def ready(self, backend):
        store = PersonaStore(backend)
        await store.open()
        await store.create_state(self.state())
        definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10},
        )
        instance = create_activity_instance(
            definition, activity_id="a-001", agent_id="persona-001", mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        await store.create_activity(definition, instance)
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        service = ActivityService("persona-001", store, ActivityManager(registry), clock=lambda: self.now)
        started = await service.start_activity(instance.activity_id)
        return store, service, started, build_activity_tick(started)

    async def test_dependency_direction_has_no_infrastructure_business_imports(self):
        root = Path(__file__).resolve().parents[1]
        for folder, forbidden in (
            ("infrastructure", {"activity", "core", "application", "persona"}),
            ("core", {"activity", "application", "infrastructure"}),
            ("activity", {"application", "infrastructure"}),
            ("persistence", {"activity", "core", "application", "infrastructure", "persona"}),
        ):
            for path in (root / folder).rglob("*.py"):
                with self.subTest(path=path):
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                    imported = set()
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ImportFrom) and node.module:
                            imported.add(node.module.split(".")[0])
                        if isinstance(node, ast.Import):
                            imported.update(alias.name.split(".")[0] for alias in node.names)
                    self.assertFalse(imported & forbidden)

    async def test_sqlite_adapter_loads_when_business_imports_are_blocked(self):
        code = """
import builtins
import importlib.util
original = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    # Resolve relative imports in the plugin namespace without blocking aiosqlite.core.
    absolute = importlib.util.resolve_name('.' * level + name, globals['__package__']) if level else name
    if any(absolute == 'persona_agent.' + p or absolute.startswith('persona_agent.' + p + '.')
           for p in ('activity', 'core', 'application', 'persona')):
        raise AssertionError(name)
    return original(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
for package in ('persona_agent.activity', 'persona_agent.core', 'persona_agent.application', 'persona_agent.persona'):
    try:
        __import__(package)
    except AssertionError:
        pass
    else:
        raise AssertionError('Business import guard is inactive: ' + package)
from persona_agent.infrastructure.storage import SQLiteStorage
assert not hasattr(SQLiteStorage, 'commit_activity_result')
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_adapter_treats_business_json_as_opaque_text(self):
        async with self.backend.transaction() as session:
            await session.insert("agent_states", {"agent_id": "opaque", "version": 0, "state_json": "not even JSON"})
        async with self.backend.transaction() as session:
            row = await session.fetch_one("agent_states", where={"agent_id": "opaque"})
        self.assertEqual(row["state_json"], "not even JSON")

    async def test_adapter_rolls_back_multiple_writes(self):
        with self.assertRaises(RuntimeError):
            async with self.backend.transaction() as session:
                await session.insert("agent_events", {"event_id": "one", "event_json": "{}"})
                await session.insert("agent_events", {"event_id": "two", "event_json": "{}"})
                raise RuntimeError("rollback")
        async with self.backend.transaction() as session:
            self.assertEqual(await session.fetch_all("agent_events"), ())

    async def test_adapter_supports_conditional_update_without_computing_versions(self):
        async with self.backend.transaction() as session:
            await session.insert("agent_states", {"agent_id": "a", "version": 0, "state_json": "{}"})
            self.assertEqual(await session.update("agent_states", {"version": 7}, where={"agent_id": "a", "version": 0}), 1)
            self.assertEqual(await session.update("agent_states", {"version": 8}, where={"agent_id": "a", "version": 0}), 0)

    async def test_transaction_session_cannot_be_reused(self):
        async with self.backend.transaction() as session:
            await session.fetch_all("agent_events")
        with self.assertRaises(StorageNotOpenError):
            await session.fetch_all("agent_events")

    async def test_adapter_rejects_unknown_identifiers(self):
        async with self.backend.transaction() as session:
            with self.assertRaises(ValueError):
                await session.fetch_all("agent_events; DROP TABLE agent_events")
            with self.assertRaises(ValueError):
                await session.fetch_all("agent_events", order_by=("unknown",))
            with self.assertRaises(ValueError):
                await session.update("agent_events", {"event_json": "{}"}, where={})

    async def test_cancelled_transaction_rolls_back(self):
        with self.assertRaises(asyncio.CancelledError):
            async with self.backend.transaction() as session:
                await session.insert("agent_events", {"event_id": "cancelled", "event_json": "{}"})
                raise asyncio.CancelledError()
        async with self.backend.transaction() as session:
            self.assertIsNone(await session.fetch_one("agent_events", where={"event_id": "cancelled"}))

    async def test_core_rules_work_with_memory_port_without_activity_repository(self):
        backend = MemoryUnitOfWork()
        await backend.open()
        async with backend.transaction() as session:
            core = CorePersistence(session)
            await core.create_state(self.state())
            await core.record_event(AgentEvent(
                event_id="event", event_type="INTERNAL.needs_update", source="test",
                timestamp=self.time, provenance="observed", payload={},
            ))
            proposal = StateChangeProposal(
                proposal_id="proposal", agent_id="persona-001", event_id="event",
                expected_version=0, needs_delta={"energy": 10},
            )
            first = await core.commit_proposal(proposal, settled_at=self.time)
            second = await core.commit_proposal(proposal, settled_at=self.time)
        self.assertEqual(first, second)
        self.assertEqual((first.needs.energy, first.version), (50, 1))

    async def test_activity_completion_works_with_memory_unit_of_work(self):
        store, service, _, plan = await self.ready(MemoryUnitOfWork())
        self.now += timedelta(seconds=10)
        self.assertEqual((await service.handle_event(plan.event)).instance.status, ActivityStatus.COMPLETED)
        state = await store.get_state("persona-001")
        self.assertEqual((state.needs.energy, state.version), (50, 1))
        await store.close()

    async def test_tick_exhaustion_works_with_memory_unit_of_work(self):
        store, _, _, plan = await self.ready(MemoryUnitOfWork())
        for attempt in range(3):
            await store.record_schedule_delivery(
                "persona-001", plan.schedule_id, expected_attempts=attempt,
                attempted_at=self.time + timedelta(seconds=5 * (attempt + 1)), succeeded=False, error="test failure",
            )
        self.assertEqual((await store.get_activity("persona-001", "a-001")).instance.status, ActivityStatus.FAILED)
        self.assertEqual(await store.get_state("persona-001"), self.state())
        await store.close()

    async def test_sqlite_open_does_not_run_business_repair(self):
        store, _, started, plan = await self.ready(self.backend)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE scheduled_events SET status='failed', attempts=3, last_error='legacy', next_attempt_at=?",
                ((self.time + timedelta(seconds=15)).isoformat(timespec="microseconds"),),
            )
            connection.commit()
        await store.close()
        await self.backend.open()
        async with self.backend.transaction() as session:
            row = await session.fetch_one("activity_instances", where={"activity_id": "a-001"})
        self.assertEqual(json.loads(row["instance_json"])["status"], "running")
        await store.open()
        self.assertEqual((await store.get_activity("persona-001", "a-001")).instance.status, ActivityStatus.FAILED)
        self.assertEqual(started.instance.revision, 1)
        self.assertEqual((await store.get_schedule("persona-001", plan.schedule_id)).attempts, 3)

    async def test_memory_transaction_rolls_back_completion_when_reward_insert_fails(self):
        backend = MemoryUnitOfWork()
        store, service, started, plan = await self.ready(backend)
        self.now += timedelta(seconds=10)
        backend.fail_table = "state_settlements"
        with self.assertRaisesRegex(RuntimeError, "injected insert failure"):
            await service.handle_event(plan.event)
        self.assertEqual(await store.get_activity("persona-001", "a-001"), started)
        self.assertEqual(await store.get_state("persona-001"), self.state())
        self.assertIsNone(await store.get_event(plan.event.event_id))
        await store.close()

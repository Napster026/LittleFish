"""核心持久化业务集成测试，组合 SQLite 事务适配器，由开发者手动运行。"""

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import AgentEvent
from core.settlement import SettlementTimeError, StateChangeProposal, StateVersionConflictError
from core.state import AgentState
from infrastructure.storage import SQLiteStorage
from application.persistence import PersonaStore
from persistence.errors import (
    EventAlreadySettledError,
    EventConflictError,
    EventNotFoundError,
    ProposalConflictError,
    StateAlreadyExistsError,
    StateNotFoundError,
    StorageNotOpenError,
    UnsupportedSchemaError,
)
from persona.needs import NeedState


class CorePersistenceTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.storage.close()
        self.directory.cleanup()

    def event(self, event_id="event-001", **updates):
        data = dict(
            event_id=event_id, event_type="ACTIVITY.completed", timestamp=self.time,
            source="activity.simulated", provenance="simulated",
            payload={"definition_id": "rest", "progress": 1.0},
        )
        data.update(updates)
        return AgentEvent(**data)

    def proposal(self, **updates):
        data = dict(
            proposal_id="proposal-001", event_id="event-001", agent_id="persona-001",
            expected_version=0, needs_delta={"energy": 10},
        )
        data.update(updates)
        return StateChangeProposal(**data)

    async def prepare(self):
        await self.storage.create_state(self.state)
        await self.storage.record_event(self.event())

    async def commit(self, proposal=None):
        return await self.storage.commit_proposal(
            self.proposal() if proposal is None else proposal,
            settled_at=self.time + timedelta(seconds=1),
        )

    async def test_state_survives_reopening(self):
        await self.storage.create_state(self.state)
        await self.storage.close()
        async with PersonaStore(SQLiteStorage(self.path)) as reopened:
            self.assertEqual(await reopened.get_state(self.state.agent_id), self.state)

    async def test_missing_reads_return_none(self):
        self.assertIsNone(await self.storage.get_state("missing"))
        self.assertIsNone(await self.storage.get_event("missing"))

    async def test_initialization_cannot_overwrite_existing_state(self):
        await self.storage.create_state(self.state)
        data = self.state.model_dump()
        data["needs"]["energy"] = 99
        with self.assertRaises(StateAlreadyExistsError):
            await self.storage.create_state(AgentState(**data))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_event_retries_ignore_dictionary_key_order(self):
        await self.storage.record_event(self.event())
        await self.storage.record_event(self.event(payload={"progress": 1.0, "definition_id": "rest"}))
        self.assertEqual(await self.storage.get_event("event-001"), self.event())

    async def test_event_id_cannot_change_content(self):
        await self.storage.record_event(self.event())
        with self.assertRaises(EventConflictError):
            await self.storage.record_event(self.event(payload={"definition_id": "other"}))
        self.assertEqual(await self.storage.get_event("event-001"), self.event())

    async def test_committed_state_and_deduplication_survive_reopening(self):
        await self.prepare()
        result = await self.commit()
        self.assertEqual(result.needs.energy, 50)
        self.assertEqual(result.version, 1)
        await self.storage.close()
        # 模拟旧 JSON：默认时间字段尚不存在，重启和原提案重试仍应兼容。
        with closing(sqlite3.connect(self.path)) as connection:
            state = json.loads(result.model_dump_json())
            state.pop("needs_updated_at")
            state.pop("emotion")
            proposal = json.loads(self.proposal().model_dump_json())
            proposal.pop("needs_time")
            proposal.pop("emotion_change")
            connection.execute("UPDATE agent_states SET state_json = ?", (json.dumps(state),))
            connection.execute("UPDATE state_settlements SET proposal_json = ?", (json.dumps(proposal),))
            connection.commit()
        async with PersonaStore(SQLiteStorage(self.path)) as reopened:
            self.assertEqual(await reopened.get_state(self.state.agent_id), result)
            self.assertEqual(await reopened.get_event("event-001"), self.event())
            retried = await reopened.commit_proposal(self.proposal(), settled_at=self.time)
            self.assertEqual(retried, result)
            self.assertEqual(await reopened.get_state(self.state.agent_id), result)

    async def test_retry_returns_latest_state_without_rolling_back(self):
        await self.prepare()
        await self.commit()
        await self.storage.record_event(self.event("event-002"))
        latest = await self.commit(self.proposal(
            proposal_id="proposal-002", event_id="event-002", expected_version=1,
        ))
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.needs.energy, 60)
        self.assertEqual(await self.commit(), latest)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), latest)

    async def test_proposal_id_cannot_change_content(self):
        await self.prepare()
        latest = await self.commit()
        with self.assertRaises(ProposalConflictError):
            await self.commit(self.proposal(needs_delta={"energy": 20}))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), latest)

    async def test_new_proposal_id_cannot_repeat_same_event(self):
        await self.prepare()
        latest = await self.commit()
        with self.assertRaises(EventAlreadySettledError):
            await self.commit(self.proposal(proposal_id="proposal-002", expected_version=1))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), latest)

    async def test_missing_state_rejects_commit(self):
        await self.storage.record_event(self.event())
        with self.assertRaises(StateNotFoundError):
            await self.commit()

    async def test_missing_event_rejects_commit(self):
        await self.storage.create_state(self.state)
        with self.assertRaises(EventNotFoundError):
            await self.commit()
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_version_conflict_does_not_write_state(self):
        await self.prepare()
        with self.assertRaises(StateVersionConflictError):
            await self.commit(self.proposal(expected_version=1))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        # 失败不能遗留“已处理”记录，修正同一 ID 的提案后应能成功。
        self.assertEqual((await self.commit()).version, 1)

    async def test_invalid_time_does_not_mark_proposal_processed(self):
        await self.prepare()
        with self.assertRaises(SettlementTimeError):
            await self.storage.commit_proposal(
                self.proposal(), settled_at=self.time - timedelta(seconds=1),
            )
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertEqual((await self.commit()).version, 1)

    async def test_record_failure_rolls_back_state_update(self):
        await self.prepare()
        # 在更新状态之后、写结算记录时制造失败，验证整个事务回滚。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                CREATE TRIGGER fail_settlement BEFORE INSERT ON state_settlements
                BEGIN SELECT RAISE(ABORT, 'test failure'); END;
            """)
            connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.commit()
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertEqual(await self.storage.get_event("event-001"), self.event())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER fail_settlement")
            connection.commit()
        self.assertEqual((await self.commit()).version, 1)

    async def test_two_connections_cannot_apply_same_state_version_twice(self):
        await self.prepare()
        await self.storage.record_event(self.event("event-002"))
        other_proposal = self.proposal(proposal_id="proposal-002", event_id="event-002")
        async with PersonaStore(SQLiteStorage(self.path)) as other:
            results = await asyncio.gather(
                self.commit(),
                other.commit_proposal(other_proposal, settled_at=self.time + timedelta(seconds=1)),
                return_exceptions=True,
            )
        self.assertEqual(sum(isinstance(result, AgentState) for result in results), 1)
        self.assertEqual(sum(isinstance(result, StateVersionConflictError) for result in results), 1)
        latest = await self.storage.get_state(self.state.agent_id)
        self.assertEqual(latest.version, 1)
        self.assertEqual(latest.needs.energy, 50)

    async def test_one_event_can_affect_different_agents(self):
        await self.prepare()
        second_data = self.state.model_dump()
        second_data["agent_id"] = "persona-002"
        await self.storage.create_state(AgentState(**second_data))
        first = await self.commit()
        second = await self.commit(self.proposal(proposal_id="proposal-002", agent_id="persona-002"))
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 1)
        self.assertEqual(second.needs.energy, 50)

    async def test_closed_storage_rejects_operations(self):
        await self.storage.close()
        with self.assertRaises(StorageNotOpenError):
            await self.storage.get_state(self.state.agent_id)

    async def test_unsupported_schema_is_not_downgraded(self):
        await self.storage.create_state(self.state)
        await self.storage.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
        with self.assertRaises(UnsupportedSchemaError):
            await self.storage.open()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT version FROM agent_states").fetchone()[0], 0)

"""基础关系的纯规则、原子持久化与迁移；由用户运行，不访问模型或 QQ。"""

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pydantic import ValidationError
from schema_fixtures import restore_v6_layout
from persona_agent.application.persistence import PersonaStore
from persona_agent.application.relationship import relationship_signal
from persona_agent.cognition.evaluation import EvaluationSettings, RuleEvaluation
from persona_agent.core.events import AgentEvent
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.needs import NeedState
from persona_agent.social.models import RelationshipSettings, RelationshipSignal, RelationshipProfile
from persona_agent.social.persistence import RelationshipPersistence
from persona_agent.social.relationships import advance_relationship

TIME=datetime(2026,11,1,12,tzinfo=timezone.utc)
AGENT="persona-001"


def message(identifier,*,text="谢谢你",at=TIME,platform_id="LittleFish",user_id="alice",scope_id=None,group_id="",bot_id="bot"):
    scope_id=scope_id or f"{platform_id}:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or user_id}"
    return AgentEvent(event_id="qq:"+identifier,event_type="SOCIAL.message_received",timestamp=at,
        source="astrbot.aiocqhttp",provenance="observed",scope_id=scope_id,
        payload={"platform_id":platform_id,"sender_id":user_id,"group_id":group_id,"bot_id":bot_id,"text":text})


def advance(profile=None,*,at=TIME,occurred_at=None,polarity="positive",settings=None):
    return advance_relationship(profile,agent_id=AGENT,platform_id="LittleFish",user_id="alice",
        now=at,occurred_at=occurred_at or at,signal=RelationshipSignal(polarity=polarity),settings=settings or RelationshipSettings())


class RelationshipRuleTests(unittest.TestCase):
    def test_strict_settings_profiles_and_disjoint_rule_mapping(self):
        for settings in ({"cooldown_seconds":True},{"daily_affinity_limit":"1"},{"familiarity_step":float("nan")},
                         {"positive_rule_ids":["same"],"negative_rule_ids":["same"]}):
            with self.subTest(settings=settings),self.assertRaises(ValidationError):RelationshipSettings(**settings)
        profile,_=advance()
        for changes in ({"affinity":True},{"familiarity":101},{"last_interaction_at":123},
                        {"updated_at":"123"},{"budget_day":"2026-1-1"}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):
                RelationshipProfile(**{**profile.model_dump(),**changes})

    def test_reuses_matched_rule_identity_not_current_emotion(self):
        settings=RelationshipSettings()
        for rule,expected in (("thanks","positive"),("insult","negative"),("custom","neutral")):
            self.assertEqual(relationship_signal(RuleEvaluation(rule_id=rule,reason="matched_phrase"),settings).polarity,expected)
        self.assertEqual(relationship_signal(RuleEvaluation(rule_id="thanks",reason="mixed_signals"),settings).polarity,"neutral")
        self.assertEqual(relationship_signal(RuleEvaluation(rule_id="custom",reason="matched_phrase"),
            RelationshipSettings(positive_rule_ids=("custom",))).polarity,"positive")

    def test_neutral_interaction_builds_familiarity_without_affinity(self):
        profile,reason=advance(polarity="neutral")
        self.assertEqual((profile.familiarity,profile.affinity,reason),(.2,0,"updated"))
        original=profile.model_dump_json()
        cooled,reason=advance(profile,at=TIME+timedelta(seconds=1),polarity="negative")
        self.assertEqual(reason,"cooldown")
        self.assertEqual((cooled.familiarity,cooled.affinity),(.2,0))
        self.assertEqual(profile.model_dump_json(),original)

    def test_daily_caps_limit_absolute_positive_and_negative_changes(self):
        settings=RelationshipSettings(cooldown_seconds=0,daily_familiarity_limit=.6,daily_affinity_limit=.3)
        profile=None
        for i,polarity in enumerate(("positive","positive","negative","positive","negative")):
            profile,_=advance(profile,at=TIME+timedelta(seconds=i),polarity=polarity,settings=settings)
        self.assertAlmostEqual(profile.familiarity,.6)
        self.assertAlmostEqual(profile.affinity,.1)
        self.assertAlmostEqual(profile.affinity_used,.3)
        self.assertAlmostEqual(profile.familiarity_used,.6)

    def test_local_budget_day_resets_lazily_on_new_interaction(self):
        settings=RelationshipSettings(cooldown_seconds=0,daily_familiarity_limit=.2,daily_affinity_limit=.1)
        first=datetime(2026,11,1,15,59,30,tzinfo=timezone.utc)
        profile,_=advance(at=first,settings=settings)
        self.assertEqual(profile.budget_day,"2026-11-01")
        profile,_=advance(profile,at=first+timedelta(seconds=61),settings=settings)
        self.assertEqual(profile.budget_day,"2026-11-02")
        self.assertAlmostEqual(profile.familiarity,.4)
        self.assertAlmostEqual(profile.affinity,.2)
        self.assertAlmostEqual(profile.familiarity_used,.2)

    def test_older_source_does_not_rewind_or_change_profile(self):
        profile,_=advance(at=TIME+timedelta(seconds=20))
        result,reason=advance(profile,at=TIME+timedelta(seconds=21),occurred_at=TIME+timedelta(seconds=10))
        self.assertEqual((result,reason),(profile,"out_of_order"))

    def test_metrics_stop_at_boundaries_and_allow_small_actual_deltas(self):
        profile,_=advance()
        near=RelationshipProfile(**{**profile.model_dump(),"familiarity":99.95,"affinity":99.95,
            "familiarity_used":0,"affinity_used":0,"cooldown_until":None})
        final,_=advance(near)
        self.assertEqual((final.familiarity,final.affinity),(100,100))
        self.assertAlmostEqual(final.affinity_used,.05)
        low=RelationshipProfile(**{**near.model_dump(),"affinity":-99.95})
        final,_=advance(low,polarity="negative")
        self.assertEqual(final.affinity,-100)


class RelationshipStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.path=Path(self.directory.name)/"relationships.sqlite3"
        self.settings=RelationshipSettings()
        self.store=PersonaStore(SQLiteStorage(self.path),evaluation=EvaluationSettings(),relationships=self.settings)
        await self.store.open()
        self.state=AgentState(agent_id=AGENT,updated_at=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50))
        await self.store.create_state(self.state)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def ingest(self,source,*,now=None,directed=False):
        return await self.store.record_rule_message(AGENT,source,now=now or source.timestamp,directed=directed)

    async def profile(self,platform_id="LittleFish",user_id="alice"):
        return await self.store.get_relationship(AGENT,platform_id,user_id)

    async def changes(self,source):
        _,changes=await self.store.relationship_view(AGENT,source.payload["platform_id"],source.payload["sender_id"],
            source.scope_id,history=True)
        return changes

    async def count(self,table):
        async with self.store._repositories() as (core,_):return len(await core.session.fetch_all(table,limit=1000))

    async def test_new_interaction_has_explainable_delta_even_without_emotion_initialization(self):
        source=message("one")
        await self.ingest(source)
        profile=await self.profile()
        self.assertEqual((profile.familiarity,profile.affinity),(.2,.1))
        item=(await self.changes(source))[0]
        self.assertEqual((item.rule_id,item.polarity,item.reason),("thanks","positive","updated"))
        self.assertEqual((item.familiarity_delta,item.affinity_delta),(.2,.1))
        self.assertEqual(item.source_event_id,source.event_id)
        self.assertEqual(profile.last_interaction_at,TIME)

    async def test_duplicate_restart_and_config_changes_keep_first_receipt(self):
        source=message("one")
        await self.ingest(source)
        profile=await self.profile()
        receipt=(await self.changes(source))[0]
        state=await self.store.get_state(AGENT)
        await self.store.close()
        await self.store.open()
        self.store._relationships=RelationshipSettings(familiarity_step=1,cooldown_seconds=0)
        await self.ingest(source,now=TIME+timedelta(seconds=10))
        self.assertEqual(await self.profile(),profile)
        self.assertEqual(await self.changes(source),(receipt,))
        self.assertEqual(await self.store.get_state(AGENT),state)
        self.assertEqual(await self.count("relationship_receipts"),1)

    async def test_independent_identities_and_agents_have_independent_profiles(self):
        for i,(platform,user) in enumerate((("LittleFish","alice"),("other-platform","alice"),("LittleFish","bob"))):
            await self.ingest(message(str(i),platform_id=platform,user_id=user))
            self.assertEqual((await self.profile(platform,user)).familiarity,.2)
        await self.store.create_state(AgentState(**{**self.state.model_dump(),"agent_id":"other-agent"}))
        source=message("other-agent")
        await self.store.record_rule_message("other-agent",source,now=TIME)
        other=await self.store.get_relationship("other-agent","LittleFish","alice")
        self.assertEqual(other.familiarity,.2)
        self.assertEqual((await self.profile()).familiarity,.2)

    async def test_cooldown_is_shared_across_scopes_but_history_is_not(self):
        first=message("first",scope_id="private-a")
        second=message("second",scope_id="private-b",at=TIME+timedelta(seconds=60))
        await self.ingest(first)
        await self.ingest(second)
        self.assertEqual((await self.profile()).familiarity,.2)
        self.assertEqual([item.source_event_id for item in await self.changes(first)],[first.event_id])
        changes=await self.changes(second)
        self.assertEqual([item.source_event_id for item in changes],[second.event_id])
        self.assertEqual(changes[0].reason,"cooldown")
        self.assertEqual((await self.profile()).last_interaction_at,second.timestamp)

    async def test_relationship_does_not_add_need_or_emotion_settlements(self):
        baseline=PersonaStore(SQLiteStorage(Path(self.directory.name)/"baseline.sqlite3"),evaluation=EvaluationSettings())
        await baseline.open()
        try:
            await baseline.create_state(self.state)
            for i,text in enumerate(("谢谢你","你真没用","聊聊今天的事情")):
                source=message(str(i),text=text,at=TIME+timedelta(seconds=i*301))
                await self.ingest(source)
                await baseline.record_rule_message(AGENT,source,now=source.timestamp)
            self.assertEqual(await self.store.get_state(AGENT),await baseline.get_state(AGENT))
            async with self.store._repositories() as (core,_),baseline._repositories() as (other,_):
                for table in ("agent_events","state_settlements","scheduled_events"):
                    self.assertEqual(await core.session.fetch_all(table),await other.session.fetch_all(table))
        finally:await baseline.close()

    async def test_daily_caps_and_disabled_evaluation_have_no_extra_rule_call(self):
        self.store._relationships=RelationshipSettings(cooldown_seconds=0,daily_familiarity_limit=.4,daily_affinity_limit=.1)
        for i in range(5):await self.ingest(message(str(i),at=TIME+timedelta(seconds=i)))
        self.assertAlmostEqual((await self.profile()).familiarity,.4)
        self.assertAlmostEqual((await self.profile()).affinity,.1)
        self.store._evaluation=EvaluationSettings(enabled=False)
        await self.ingest(message("neutral",user_id="new-user",text="谢谢你",at=TIME+timedelta(seconds=5)))
        profile=await self.profile(user_id="new-user")
        self.assertEqual((profile.familiarity,profile.affinity),(.2,0))

    async def test_disabled_and_historical_sources_do_not_backfill_when_enabled(self):
        self.store._relationships=RelationshipSettings(enabled=False)
        source=message("disabled")
        await self.ingest(source)
        self.store._relationships=self.settings
        await self.ingest(source)
        self.assertIsNone(await self.profile())
        old=message("history")
        await self.store.record_event(old)
        await self.ingest(old)
        self.assertIsNone(await self.profile())
        self.assertEqual({item.reason for item in await self.changes(old)},{"disabled","history_not_backfilled"})
        await self.ingest(message("fresh"))
        self.assertIsNotNone(await self.profile())

    async def test_group_opt_in_still_requires_directed_message(self):
        blocked=message("group-default",group_id="group")
        await self.ingest(blocked,directed=True)
        self.assertIsNone(await self.profile())
        self.store._relationships=RelationshipSettings(private_only=False)
        undirected=message("group-undirected",group_id="group",at=TIME+timedelta(seconds=1))
        await self.ingest(undirected)
        self.assertIsNone(await self.profile())
        directed=message("group-directed",group_id="group",at=TIME+timedelta(seconds=2))
        await self.ingest(directed,directed=True)
        self.assertEqual((await self.profile()).familiarity,.2)
        self.assertEqual({item.reason for item in await self.changes(directed)},{"private_only","not_directed","updated"})

    async def test_empty_command_self_missing_identity_and_expired_sources_are_ignored(self):
        sources=(message("empty",text=" "),message("command",text="/persona_status"),
                 message("self",user_id="bot"),message("missing",platform_id=""))
        for source in sources:await self.ingest(source)
        old=message("expired")
        await self.ingest(old,now=TIME+timedelta(seconds=301))
        self.assertIsNone(await self.profile())
        self.assertEqual(await self.count("relationships"),0)
        self.assertEqual(await self.count("relationship_receipts"),5)

    async def test_out_of_order_message_does_not_replace_last_interaction_or_metrics(self):
        await self.ingest(message("newer",at=TIME+timedelta(seconds=20)))
        profile=await self.profile()
        old=message("older",at=TIME+timedelta(seconds=10))
        await self.ingest(old,now=TIME+timedelta(seconds=21))
        self.assertEqual(await self.profile(),profile)
        self.assertEqual((await self.changes(old))[0].reason,"out_of_order")

    async def test_receipt_write_failure_rolls_back_message_evaluation_profile_and_state(self):
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.insert
        async def failed(session,table,values):
            if table=="relationship_receipts":raise RuntimeError("receipt failure")
            return await original(session,table,values)
        source=message("atomic")
        with patch.object(kind,"insert",new=failed):
            with self.assertRaises(RuntimeError):await self.ingest(source)
        self.assertIsNone(await self.store.get_event(source.event_id))
        self.assertIsNone(await self.profile())
        self.assertEqual(await self.store.get_state(AGENT),self.state)
        self.assertEqual(await self.count("state_settlements"),0)
        await self.ingest(source)
        self.assertEqual((await self.profile()).familiarity,.2)

    async def test_history_is_bounded_indexed_and_contains_no_message_body(self):
        source=message("private",text="谢谢你 PRIVATE_MESSAGE_BODY")
        await self.ingest(source)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.fetch_all
        async def guarded(session,table,**kwargs):
            self.assertIn(table,("relationships","relationship_receipts"))
            where=kwargs.get("where") or {}
            self.assertEqual(where.get("agent_id"),AGENT)
            self.assertEqual((where.get("platform_id"),where.get("user_id")),("LittleFish","alice"))
            if table=="relationship_receipts":
                self.assertIn(where.get("scope_id"),(source.scope_id,""))
                self.assertLessEqual(kwargs.get("limit",100),10)
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=guarded):
            _,changes=await self.store.relationship_view(AGENT,"LittleFish","alice",source.scope_id,history=True)
        self.assertEqual(len(changes),1)
        self.assertNotIn("PRIVATE_MESSAGE_BODY",changes[0].model_dump_json())

    async def test_v6_upgrade_preserves_existing_business_json_without_backfill(self):
        source=message("legacy")
        await self.store.record_event(source)
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            before=db.execute("SELECT state_json FROM agent_states").fetchone()[0]
            raw=db.execute("SELECT event_json FROM agent_events WHERE event_id=?",(source.event_id,)).fetchone()[0]
            restore_v6_layout(db)
            db.commit()
        await self.store.open()
        self.assertEqual(await self.store.get_state(AGENT),self.state)
        self.assertEqual(await self.store.get_event(source.event_id),source)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state_json FROM agent_states").fetchone()[0],before)
        await self.ingest(source)
        self.assertIsNone(await self.profile())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)
            self.assertEqual(len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()),16)
            # 入站重放可能复用既有评价；此处只核对原始事件 JSON。
            self.assertEqual(db.execute("SELECT event_json FROM agent_events WHERE event_id=?",(source.event_id,)).fetchone()[0],raw)

    async def test_failed_v6_upgrade_rolls_back_both_new_table_and_version_then_retries(self):
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            restore_v6_layout(db)
            db.execute("CREATE TABLE relationship_receipts (collision TEXT)")
            db.commit()
        with self.assertRaises(sqlite3.OperationalError):await self.store.open()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],6)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='relationships'").fetchone())
            db.execute("DROP TABLE relationship_receipts")
            db.commit()
        await self.store.open()
        self.assertEqual(await self.store.get_state(AGENT),self.state)
        self.assertIsNone(await self.profile())

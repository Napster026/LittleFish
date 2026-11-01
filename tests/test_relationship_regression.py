"""每日倾向回归、日历时钟、离线补算及独立结算；由用户运行，不调用模型。"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pydantic import ValidationError
from persona_agent.application.persistence import PersonaStore
from persona_agent.cognition.evaluation import EvaluationSettings
from persona_agent.cognition.prompt_builder import ChatContextSettings, build_prompt_context
from persona_agent.core.events import AgentEvent
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.needs import NeedState, NeedRates
from persona_agent.social.clock import build_relationship_day, relationship_day_plans, parse_relationship_day
from persona_agent.social.models import RelationshipProfile, RelationshipSettings, RelationshipSignal
from persona_agent.social.persistence import RelationshipPersistence, REGRESSION_BATCH_SIZE
from persona_agent.social.relationships import advance_relationship, regress_affinity, regress_relationship

TIME=datetime(2026,11,1,12,tzinfo=timezone.utc)
AGENT="persona-001"
SCOPE="LittleFish:FriendMessage:alice"


def profile(*,affinity=-1,user_id="alice",platform_id="LittleFish",agent_id=AGENT,**changes):
    return RelationshipProfile(**{**dict(agent_id=agent_id,platform_id=platform_id,user_id=user_id,
        familiarity=5,affinity=affinity,last_interaction_at=TIME,cooldown_until=TIME+timedelta(seconds=300),
        budget_day="2026-11-01",affinity_day="2026-11-01",familiarity_used=.4,affinity_used=.2,
        revision=1,updated_at=TIME),**changes})


class AffinityRegressionRuleTests(unittest.TestCase):
    def test_negative_formula_and_zero_clamp(self):
        settings=RelationshipSettings()
        for before,after in ((-100,-50),(-1,-.5),(-.15,-.05),(-.05,0)):
            with self.subTest(before=before):
                self.assertAlmostEqual(regress_affinity(before,1,settings),after)
                self.assertLessEqual(regress_affinity(before,1,settings),0)

    def test_positive_goodwill_is_preserved_and_only_excess_decays(self):
        settings=RelationshipSettings()
        for value in (0,.1,.49,.5):self.assertEqual(regress_affinity(value,100,settings),value)
        self.assertEqual(regress_affinity(1,1,settings),.75)
        self.assertEqual(regress_affinity(1,2,settings),.625)
        self.assertEqual(regress_affinity(3,5,settings),.578125)
        self.assertEqual(regress_affinity(100,1000000,settings),.5)

    def test_multiple_negative_days_are_folded_without_crossing_positive(self):
        settings=RelationshipSettings()
        for days,expected in ((1,-.5),(2,-.25),(3,-.125),(4,-.025),(5,0),(1000000,0)):
            self.assertAlmostEqual(regress_affinity(-1,days,settings),expected)
        self.assertEqual(regress_affinity(-1,3,RelationshipSettings(affinity_daily_retention=1)), -.7)
        self.assertEqual(regress_affinity(-1,2,RelationshipSettings(affinity_negative_recovery=0)), -.25)
        self.assertEqual(regress_affinity(-1,1,RelationshipSettings(affinity_daily_retention=0)),0)

    def test_daily_cursor_preserves_interaction_time_cooldown_and_budget(self):
        before=profile()
        after,from_day,days=regress_relationship(before,now=TIME+timedelta(days=2),settings=RelationshipSettings())
        self.assertEqual((after.affinity,after.affinity_day,from_day,days),(-.25,"2026-11-03","2026-11-01",2))
        for field in ("familiarity","last_interaction_at","cooldown_until","budget_day","familiarity_used","affinity_used"):
            self.assertEqual(getattr(after,field),getattr(before,field))
        self.assertEqual(after.revision,before.revision+1)
        self.assertEqual(before.affinity,-1)
        repeated,_,days=regress_relationship(after,now=TIME+timedelta(days=2,hours=1),settings=RelationshipSettings())
        self.assertEqual(repeated,after)
        self.assertEqual(days,0)

    def test_legacy_cursor_anchors_to_last_known_update_day(self):
        old=profile(affinity_day=None)
        same,_,days=regress_relationship(old,now=TIME,settings=RelationshipSettings())
        self.assertEqual((same.affinity,same.affinity_day,days),(-1,"2026-11-01",0))
        after,_,days=regress_relationship(old,now=TIME+timedelta(days=2),settings=RelationshipSettings())
        self.assertEqual((after.affinity,days),(-.25,2))

    def test_local_settlement_hour_and_backwards_time_do_not_replay_a_day(self):
        at=datetime(2026,11,1,19,59,30,tzinfo=timezone.utc)
        before=profile(updated_at=at,last_interaction_at=at,cooldown_until=None)
        after,_,days=regress_relationship(before,now=at+timedelta(seconds=31),settings=RelationshipSettings())
        self.assertEqual((after.affinity,after.affinity_day,days),(-.5,"2026-11-02",1))
        again,_,days=regress_relationship(after,now=at,settings=RelationshipSettings())
        self.assertEqual((again,days),(after,0))

    def test_four_am_default_and_six_am_option_wait_for_settlement_boundary(self):
        before=profile()
        for hour in (4,6):
            with self.subTest(hour=hour):
                settings=RelationshipSettings(affinity_settlement_hour=hour)
                just_before=datetime(2026,11,1,16+hour-1,59,tzinfo=timezone.utc)
                unchanged,_,days=regress_relationship(before,now=just_before,settings=settings)
                self.assertEqual((unchanged,days),(before,0))
                at=datetime(2026,11,1,16+hour,tzinfo=timezone.utc)
                after,_,days=regress_relationship(before,now=at,settings=settings)
                self.assertEqual((after.affinity,days),(-.5,1))
                _,plan=relationship_day_plans(AGENT,just_before,settings)
                self.assertEqual(plan.due_at,at)
                self.assertEqual(parse_relationship_day(plan.event).settlement_hour,hour)

    def test_disabled_and_invalid_policy(self):
        before=profile()
        for settings in (RelationshipSettings(enabled=False),RelationshipSettings(affinity_regression_enabled=False)):
            self.assertEqual(regress_relationship(before,now=TIME+timedelta(days=5),settings=settings)[0],before)
            self.assertEqual(regress_affinity(-1,5,settings),-1)
        for changes in ({"affinity_settlement_hour":True},{"affinity_settlement_hour":24},
                        {"affinity_daily_retention":True},{"affinity_daily_retention":1.1},
                        {"affinity_negative_recovery":"0.1"},{"affinity_positive_baseline":float("nan")}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):RelationshipSettings(**changes)

    def test_new_profile_before_four_am_uses_previous_settlement_day(self):
        at=datetime(2026,11,1,19,30,tzinfo=timezone.utc)
        settings=RelationshipSettings()
        new,_=advance_relationship(None,agent_id=AGENT,platform_id="LittleFish",user_id="new-user",
            occurred_at=at,now=at,signal=RelationshipSignal(polarity="negative"),settings=settings)
        self.assertEqual((new.budget_day,new.affinity_day),("2026-11-02","2026-11-01"))
        after,_,days=regress_relationship(new,now=at+timedelta(minutes=31),settings=settings)
        self.assertEqual((after.affinity,days),(-.1,1))
        self.assertEqual(after.affinity_used,new.affinity_used)

    def test_legacy_midnight_event_is_stable_and_rejects_tampering(self):
        first=build_relationship_day(AGENT,"2026-11-02",480)
        self.assertEqual(first,build_relationship_day(AGENT,"2026-11-02",480))
        self.assertEqual(first.due_at,datetime(2026,11,1,16,tzinfo=timezone.utc))
        self.assertEqual(parse_relationship_day(first.event).day,"2026-11-02")
        bad=AgentEvent(**{**first.event.model_dump(),"source":"someone-else"})
        with self.assertRaises(ValueError):parse_relationship_day(bad)


class AffinityRegressionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.settings=RelationshipSettings()
        self.store=PersonaStore(SQLiteStorage(Path(self.directory.name)/"daily.sqlite3"),
            evaluation=EvaluationSettings(),relationships=self.settings)
        await self.store.open()
        self.state=AgentState(agent_id=AGENT,updated_at=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50))
        await self.store.create_state(self.state)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def seed(self,item=None):
        item=item or profile()
        async with self.store._repositories() as (core,_):await core.session.insert("relationships",RelationshipPersistence.row(item))
        return item

    async def current(self):return await self.store.get_relationship(AGENT,"LittleFish","alice")

    async def rows(self,table):
        async with self.store._repositories() as (core,_):return await core.session.fetch_all(table,limit=1000)

    async def test_daily_delivery_replay_and_restart_do_not_repeat_adjustment(self):
        original=await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        now=TIME+timedelta(days=1)
        report=await self.store.handle_relationship_day(AGENT,plan.event,now=now)
        self.assertEqual(report["changed"],1)
        current=await self.current()
        self.assertEqual(current.affinity,-.5)
        self.assertEqual(current.last_interaction_at,original.last_interaction_at)
        _,changes=await self.store.relationship_view(AGENT,"LittleFish","alice",SCOPE,history=True)
        self.assertEqual((len(changes),changes[0].kind,changes[0].days),(1,"regression",1))
        self.assertEqual(changes[0].affinity_delta,.5)
        self.assertEqual(changes[0].familiarity_delta,0)
        await self.store.handle_relationship_day(AGENT,plan.event,now=now+timedelta(hours=1))
        await self.store.close()
        await self.store.open()
        await self.store.start_relationship_clock(AGENT,now=now+timedelta(hours=1))
        self.assertEqual(await self.current(),current)
        self.assertEqual(len(await self.rows("relationship_receipts")),1)
        self.assertEqual(await self.store.get_state(AGENT),self.state)
        self.assertEqual(await self.rows("state_settlements"),())

    async def test_offline_days_fold_into_one_numeric_receipt(self):
        await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        report=await self.store.start_relationship_clock(AGENT,now=TIME+timedelta(days=5))
        self.assertEqual(report["changed"],1)
        self.assertEqual((await self.current()).affinity,0)
        _,changes=await self.store.relationship_view(AGENT,"LittleFish","alice",SCOPE,history=True)
        self.assertEqual((len(changes),changes[0].days),(1,5))
        events=[json.loads(row["event_json"]) for row in await self.rows("agent_events")]
        self.assertEqual(sum(event["event_type"]=="INTERNAL.relationship_regressed" for event in events),1)
        self.assertEqual(await self.store.get_state(AGENT),self.state)

    async def test_new_day_message_regresses_old_value_before_adding_new_affinity(self):
        old=await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        now=TIME+timedelta(days=1)
        source=AgentEvent(event_id="qq:fresh",event_type="SOCIAL.message_received",timestamp=now,
            source="astrbot.aiocqhttp",provenance="observed",scope_id=SCOPE,
            payload={"platform_id":"LittleFish","sender_id":"alice","group_id":"","bot_id":"bot","text":"谢谢你"})
        await self.store.record_rule_message(AGENT,source,now=now)
        current=await self.current()
        self.assertAlmostEqual(current.affinity,-.4)
        self.assertAlmostEqual(current.affinity_used,.1)
        self.assertAlmostEqual(current.familiarity,old.familiarity+.2)
        self.assertEqual(current.last_interaction_at,now)
        _,changes=await self.store.relationship_view(AGENT,"LittleFish","alice",SCOPE,history=True)
        self.assertEqual([item.kind for item in changes],["interaction","regression"])
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        await self.store.handle_relationship_day(AGENT,plan.event,now=now+timedelta(seconds=10))
        self.assertEqual(await self.current(),current)
        self.assertEqual(len(await self.rows("relationship_receipts")),2)

    async def test_overnight_message_before_four_am_does_not_regress_early(self):
        await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        now=datetime(2026,11,1,19,30,tzinfo=timezone.utc)  # UTC+8 的次日 03:30
        source=AgentEvent(event_id="qq:overnight",event_type="SOCIAL.message_received",timestamp=now,
            source="astrbot.aiocqhttp",provenance="observed",scope_id=SCOPE,
            payload={"platform_id":"LittleFish","sender_id":"alice","group_id":"","text":"谢谢你"})
        await self.store.record_rule_message(AGENT,source,now=now)
        current=await self.current()
        self.assertAlmostEqual(current.affinity,-.9)
        self.assertEqual(current.affinity_day,"2026-11-01")
        self.assertEqual(len(await self.rows("relationship_receipts")),1)
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        await self.store.handle_relationship_day(AGENT,plan.event,now=now+timedelta(minutes=31))
        self.assertAlmostEqual((await self.current()).affinity,-.45)
        self.assertEqual(len(await self.rows("relationship_receipts")),2)

    async def test_stable_goodwill_advances_cursor_without_per_user_empty_events(self):
        await self.seed(profile(affinity=.4))
        report=await self.store.start_relationship_clock(AGENT,now=TIME+timedelta(days=3))
        self.assertEqual((report["updated"],report["changed"]),(1,0))
        self.assertEqual((await self.current()).affinity,.4)
        self.assertEqual((await self.current()).affinity_day,"2026-11-04")
        self.assertEqual(await self.rows("relationship_receipts"),())
        events=[json.loads(row["event_json"]) for row in await self.rows("agent_events")]
        self.assertFalse(any(event["event_type"]=="INTERNAL.relationship_regressed" for event in events))
        self.assertEqual(await self.store.get_state(AGENT),self.state)

    async def test_context_preserves_small_negative_regression_value(self):
        await self.seed()
        now=TIME+timedelta(days=4)
        await self.store.start_relationship_clock(AGENT,now=now)
        snapshot=await self.store.chat_snapshot(AGENT,SCOPE,now=now,
            rates=NeedRates(energy=0,satiety=0,boredom=0,social_need=0),labels={},
            speaker_id="LittleFish:user:alice",platform_id="LittleFish",user_id="alice")
        context=build_prompt_context(snapshot,ChatContextSettings())
        data=json.loads(context.user_text.removeprefix("persona_state="))
        self.assertEqual(data["relationship"]["affinity"],-.025)
        self.assertEqual((await self.current()).last_interaction_at,TIME)

    async def test_daily_receipts_are_visible_for_same_user_but_do_not_cross_users(self):
        await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME+timedelta(days=1))
        for scope in (SCOPE,"another-private"):
            _,changes=await self.store.relationship_view(AGENT,"LittleFish","alice",scope,history=True)
            self.assertEqual([item.reason for item in changes],["daily_regression"])
        _,others=await self.store.relationship_view(AGENT,"LittleFish","bob",SCOPE,history=True)
        self.assertEqual(others,())

    async def test_next_schedule_failure_rolls_back_regression_receipt_and_clock_event(self):
        original_profile=await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.insert
        async def failed(session,table,values):
            if table=="scheduled_events":raise RuntimeError("next schedule failed")
            return await original(session,table,values)
        with patch.object(kind,"insert",new=failed):
            with self.assertRaises(RuntimeError):
                await self.store.handle_relationship_day(AGENT,plan.event,now=TIME+timedelta(days=1))
        self.assertEqual(await self.current(),original_profile)
        self.assertIsNone(await self.store.get_event(plan.event.event_id))
        self.assertEqual(await self.rows("relationship_receipts"),())
        await self.store.handle_relationship_day(AGENT,plan.event,now=TIME+timedelta(days=1))
        self.assertEqual((await self.current()).affinity,-.5)

    async def test_disabled_clock_does_not_regress_or_extend_chain(self):
        before=await self.seed()
        await self.store.start_relationship_clock(AGENT,now=TIME)
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        self.store._relationships=RelationshipSettings(affinity_regression_enabled=False)
        await self.store.handle_relationship_day(AGENT,plan.event,now=TIME+timedelta(days=1))
        self.assertEqual(await self.current(),before)
        _,future=relationship_day_plans(AGENT,TIME+timedelta(days=1),self.settings)
        self.assertIsNone(await self.store.get_schedule(AGENT,future.schedule_id))
        self.assertEqual(await self.rows("relationship_receipts"),())

    async def test_legacy_midnight_plan_waits_for_new_four_am_boundary(self):
        original=await self.seed()
        legacy=build_relationship_day(AGENT,"2026-11-02",480)
        await self.store.create_schedule(legacy)
        one_am=datetime(2026,11,1,17,tzinfo=timezone.utc)
        await self.store.handle_relationship_day(AGENT,legacy.event,now=one_am)
        self.assertEqual(await self.current(),original)
        _,four_am=relationship_day_plans(AGENT,one_am,self.settings)
        self.assertIsNotNone(await self.store.get_schedule(AGENT,four_am.schedule_id))
        await self.store.handle_relationship_day(AGENT,four_am.event,now=four_am.due_at)
        current=await self.current()
        self.assertEqual(current.affinity,-.5)
        await self.store.handle_relationship_day(AGENT,legacy.event,now=four_am.due_at+timedelta(minutes=1))
        self.assertEqual(await self.current(),current)
        self.assertEqual(len(await self.rows("relationship_receipts")),1)

    async def test_unknown_or_future_clock_event_is_rejected(self):
        _,plan=relationship_day_plans(AGENT,TIME,self.settings)
        with self.assertRaises(ValueError):await self.store.handle_relationship_day(AGENT,plan.event,now=TIME)
        with self.assertRaises(ValueError):await self.store.handle_relationship_day(AGENT,plan.event,now=TIME+timedelta(days=1))

    async def test_bad_profile_is_skipped_without_blocking_healthy_profiles_or_clock(self):
        await self.seed()
        bad=profile(user_id="broken")
        row=RelationshipPersistence.row(bad)
        row["profile_json"]="{PRIVATE_BROKEN_JSON"
        async with self.store._repositories() as (core,_):await core.session.insert("relationships",row)
        with self.assertLogs("persona_agent.social.persistence",level="WARNING") as captured:
            report=await self.store.start_relationship_clock(AGENT,now=TIME+timedelta(days=1))
        self.assertEqual((report["scanned"],report["changed"],report["skipped"]),(2,1,1))
        self.assertEqual((await self.current()).affinity,-.5)
        self.assertNotIn("PRIVATE_BROKEN_JSON"," ".join(captured.output))
        async with self.store._repositories() as (core,_):
            stored=await core.session.fetch_one("relationships",where={"agent_id":AGENT,"platform_id":"LittleFish","user_id":"broken"})
        self.assertEqual(stored,row)

    async def test_batch_and_platform_cursors_cover_more_than_one_page(self):
        async with self.store._repositories() as (core,_):
            for index in range(REGRESSION_BATCH_SIZE+1):
                await core.session.insert("relationships",RelationshipPersistence.row(profile(
                    affinity=.3,user_id=f"user-{index:03}",platform_id="platform-a")))
            await core.session.insert("relationships",RelationshipPersistence.row(profile(affinity=.3,platform_id="platform-b")))
            kind=type(core.session)
        original=kind.fetch_all
        async def bounded(session,table,**kwargs):
            if table=="relationships":
                self.assertLessEqual(kwargs.get("limit",1000),REGRESSION_BATCH_SIZE)
                self.assertEqual((kwargs.get("where") or {}).get("agent_id"),AGENT)
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=bounded):
            report=await self.store.start_relationship_clock(AGENT,now=TIME+timedelta(days=1))
        self.assertEqual(report["scanned"],REGRESSION_BATCH_SIZE+2)
        self.assertEqual(report["updated"],REGRESSION_BATCH_SIZE+2)
        self.assertEqual(report["changed"],0)

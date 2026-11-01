"""主动表达策略、事务与执行边界；由开发者运行。"""

import asyncio
import json
import sqlite3
import types
import unittest
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock,patch
from pydantic import ValidationError

from persona_agent.application.persistence import PersonaStore
from persona_agent.application.host import PersonaHost
from persona_agent.application.settings import HostSettings
from persona_agent.application.astrbot_expression import AstrBotExpressionAdapter
from persona_agent.core.arbiter import ArbitrationContext,BehaviorArbiter,BehaviorCandidate,BehaviorKind,ActiveBehavior
from persona_agent.core.state import AgentState
from persona_agent.activity.selection import ActivitySelector
from persona_agent.social.expression import ExpressionSettings,ExpressionTarget,quiet_time,gate
from persona_agent.social.conversation import ConversationSettings
from persona_agent.persona.needs import NeedState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.cognition.prompt_builder import ChatContextSettings,ChatSnapshot,ContextOutbound,build_prompt_context
from schema_fixtures import restore_v8_layout
from test_conversation import incoming,TIME
from test_autonomy import definition

TARGET=ExpressionTarget(platform_id="qq",user_id="alice")
NOW=TIME+timedelta(hours=2)


def settings(**kwargs):
    return ExpressionSettings(targets=(TARGET,),quiet_start_hour=0,quiet_end_hour=0,**kwargs)


class ExpressionRules(unittest.TestCase):
    def test_defaults_validation_and_quiet_boundaries(self):
        self.assertFalse(ExpressionSettings().send_enabled)
        for data in ({"send_enabled":"true"},{"daily_limit":0},{"targets":[TARGET,TARGET]},
                {"targets":[{"platform_id":"bad:id","user_id":"a"}]}):
            with self.subTest(data=data),self.assertRaises(ValidationError):ExpressionSettings(**data)
        cfg=ExpressionSettings()
        self.assertTrue(quiet_time(datetime(2026,11,1,15,tzinfo=timezone.utc),cfg))
        self.assertFalse(quiet_time(datetime(2026,11,2,0,tzinfo=timezone.utc),cfg))
        self.assertFalse(quiet_time(TIME,settings()))

    def test_expression_joins_utility_ranking_and_hard_constraints(self):
        expr=BehaviorCandidate(candidate_id="express:a",kind=BehaviorKind.EXPRESS,target_scope_id=TARGET.scope_id,utility=.8)
        rest=BehaviorCandidate(candidate_id="start:rest",kind="start_activity",definition_id="rest",mode="simulated",utility=.9)
        arbiter=BehaviorArbiter()
        self.assertEqual(arbiter.decide(ArbitrationContext(),(expr,)).winner,expr)
        self.assertEqual(arbiter.decide(ArbitrationContext(),(expr,rest)).winner,rest)
        for context in (ArbitrationContext(conversation_reason="reply_pending"),ArbitrationContext(expression_pending=True),
                ArbitrationContext(active=(ActiveBehavior(activity_id="a",definition_id="rest",mode="simulated",status="paused"),))):
            self.assertEqual(arbiter.decide(context,(expr,)).winner.kind,BehaviorKind.IDLE)
        with self.assertRaises(ValidationError):
            BehaviorCandidate(candidate_id="e",kind=BehaviorKind.EXPRESS,target_scope_id=TARGET.scope_id,activity_id="fake")

    def test_outbound_context_is_scoped_bounded_and_not_delivery_evidence(self):
        snapshot=ChatSnapshot(agent_id="agent",scope_id=TARGET.scope_id,state_version=0,as_of=NOW,
            needs=NeedState(energy=20,satiety=100,boredom=0,social_need=80),emotion=None,speaker_id="qq:user:alice",
            outbound=ContextOutbound(scope_id=TARGET.scope_id,actor_id="qq:user:alice",text="刚才的主动表达",at=NOW))
        out=build_prompt_context(snapshot,ChatContextSettings())
        self.assertIn("刚才的主动表达",out.user_text)
        self.assertIn("adapter_returned_not_delivery_receipt",out.user_text)
        other=snapshot.model_copy(update={"speaker_id":"qq:user:bob"})
        self.assertNotIn("own_outbound",build_prompt_context(other,ChatContextSettings()).user_text)
        small=build_prompt_context(snapshot,ChatContextSettings(max_chars=500))
        self.assertLessEqual(len(small.system_text)+len(small.user_text),500)


class ExpressionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory();self.path=Path(self.directory.name)/"persona.sqlite3"
        self.cfg=settings(send_enabled=True)
        self.now=NOW
        self.store=PersonaStore(SQLiteStorage(self.path),expression=self.cfg,conversation=ConversationSettings())
        await self.store.open()
        self.state=AgentState(agent_id="agent",updated_at=TIME,
            needs=NeedState(energy=100,satiety=100,boredom=0,social_need=80))
        await self.store.create_state(self.state)
        event=incoming()
        await self.store.conversation_receive("agent",event,now=TIME,settings=ConversationSettings(),directed=True)
        await self.store.record_event(event)
        self.selector=ActivitySelector(())

    async def asyncTearDown(self):
        await self.store.close();self.directory.cleanup()

    async def reserve(self,key="one"):
        return await self.store.reserve_behavior("agent",self.selector,decision_key=key,now=self.now,arbiter=BehaviorArbiter())

    async def intent(self):
        return (await self.reserve()).expression

    async def claim(self,item,phase="generate",body=""):
        return await self.store.claim_expression("agent",item.intent_id,now=self.now,autonomous_enabled=True,phase=phase,body=body)

    async def test_preview_does_not_call_model_or_hold_activity_selection(self):
        self.store._expression=settings()
        self.selector=ActivitySelector((definition("social",social_need=-30),))
        result=await self.reserve()
        head=await self.store.expression_head("agent")
        self.assertEqual(head.intent.status,"preview")
        self.assertEqual(head.used,1)
        self.assertEqual(result.decision.winner.kind,BehaviorKind.START_ACTIVITY)
        self.assertIsNone(result.expression)
        self.assertEqual(await self.store.get_state("agent"),self.state)
        await self.reserve()
        self.assertEqual((await self.store.expression_head("agent")).used,1)

    async def test_live_reservation_is_durable_and_claimed_once(self):
        item=await self.intent()
        self.assertEqual(item.status,"pending")
        self.assertEqual((await self.reserve()).expression,item)
        self.assertEqual((await self.reserve("other")).decision.reason,"expression_pending")
        self.assertIsNotNone(await self.claim(item))
        self.assertIsNone(await self.claim(item))
        self.assertEqual((await self.store.expression_head("agent")).used,1)

    async def test_recent_source_and_bound_target_are_required(self):
        self.store._expression_target_allowed=lambda target:False
        self.assertIsNone((await self.reserve()).expression)
        self.store._expression_target_allowed=None
        self.now=TIME+timedelta(seconds=10)
        self.assertIsNone((await self.reserve("recent")).expression)
        self.now=TIME+timedelta(days=8)
        self.assertIsNone((await self.reserve("old")).expression)

    async def test_new_inbound_cancels_generation_and_prevents_send_claim(self):
        item=await self.intent();await self.claim(item)
        from persona_agent.application.expression import ExpressionPersistence
        async with self.store._repositories() as (core,_):
            await ExpressionPersistence(core).cancel_inbound("agent",incoming("new",7200),self.now)
        self.assertIsNone(await self.claim(item,"send","你好"))
        self.assertEqual((await self.store.expression_head("agent")).intent.status,"cancelled")

    async def test_sending_rechecks_binding_quiet_and_autonomy(self):
        item=await self.intent();await self.claim(item)
        self.store._expression_target_allowed=lambda target:False
        self.assertIsNone(await self.claim(item,"send","你好"))
        self.assertEqual((await self.store.expression_head("agent")).intent.reason,"revalidation_failed")

    async def test_sent_opens_followup_without_needs_reward_or_fake_user_event(self):
        item=await self.intent();await self.claim(item)
        await self.claim(item,"send","最近怎么样？")
        sent=await self.store.finish_expression("agent",item.intent_id,now=self.now,status="sent")
        self.assertEqual(sent.body,"最近怎么样？")
        conv=await self.store.get_conversation("agent",TARGET.scope_id)
        self.assertEqual(conv.source_event_id,item.intent_id)
        self.assertEqual(conv.phase,"send_hook_observed")
        self.assertEqual(conv.own_outbound_event_id,item.intent_id+":sent")
        self.assertIsNone(await self.store.get_event(item.intent_id))
        self.assertEqual(await self.store.get_state("agent"),self.state)
        self.assertIsNone(await self.store.finish_expression("agent",item.intent_id,now=self.now,status="sent"))

    async def test_restart_does_not_retry_uncertain_send_or_interrupted_generation(self):
        item=await self.intent();await self.claim(item);await self.claim(item,"send","你好")
        self.assertIsNone(await self.store.recover_expression("agent",now=self.now))
        self.assertEqual((await self.store.expression_head("agent")).intent.status,"unknown")
        self.assertIsNone(await self.claim(item,"send","你好"))

    async def test_budget_cooldown_and_backwards_clock(self):
        item=await self.intent();head=await self.store.expression_head("agent")
        self.assertEqual(gate(self.cfg,self.now-timedelta(seconds=1),head),"clock_reversed")
        self.assertEqual(gate(settings(cooldown_seconds=60),self.now+timedelta(seconds=61),head),"expression_pending")
        await self.claim(item)
        await self.store.finish_expression("agent",item.intent_id,now=self.now,status="failed")
        # 模型不可变；状态提交不会更新此前读取的 pending 快照。
        head=await self.store.expression_head("agent")
        self.assertEqual(head.intent.status,"failed")
        self.assertEqual(gate(self.cfg,self.now+timedelta(seconds=120),head),"cooldown")
        from persona_agent.social.expression import ExpressionHead
        head=ExpressionHead(**{**head.model_dump(),"used":2})
        self.assertEqual(gate(settings(cooldown_seconds=60),self.now+timedelta(seconds=61),head),"daily_limit")

    async def test_expired_intent_and_autonomy_pause_cancel_without_send(self):
        item=await self.intent()
        self.now=item.expires_at
        self.assertIsNone(await self.claim(item))
        self.assertEqual((await self.store.expression_head("agent")).intent.status,"cancelled")

    async def test_interrupted_generation_is_failed_without_automatic_retry(self):
        item=await self.intent();await self.claim(item)
        self.assertIsNone(await self.store.recover_expression("agent",now=self.now))
        self.assertEqual((await self.store.expression_head("agent")).intent.status,"failed")
        self.assertIsNone(await self.claim(item))

    async def test_reservation_rolls_back_budget_and_intent_on_audit_failure(self):
        with patch("persona_agent.application.expression.ExpressionPersistence.audit",AsyncMock(side_effect=RuntimeError("fail"))):
            with self.assertRaises(RuntimeError):await self.reserve()
        self.assertIsNone(await self.store.expression_head("agent"))

    async def test_v8_upgrade_preserves_state_and_failure_rolls_back(self):
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            restore_v8_layout(db);db.execute("CREATE VIEW expression_heads AS SELECT 1");db.commit()
        with self.assertRaises(sqlite3.OperationalError):await self.store.open()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],8)
            db.execute("DROP VIEW expression_heads");db.commit()
        await self.store.open()
        self.assertEqual(await self.store.get_state("agent"),self.state)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)


class ExpressionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_target_persona_history_and_no_tools(self):
        intent=types.SimpleNamespace(target=TARGET)
        context=types.SimpleNamespace(get_config=lambda **kw:{},
            platform_manager=types.SimpleNamespace(get_insts=lambda:[types.SimpleNamespace(meta=lambda:types.SimpleNamespace(id="qq",name="aiocqhttp"))]),
            conversation_manager=types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(return_value=types.SimpleNamespace(persona_id="p",history=json.dumps([
                    {"role":"system","content":"old system"},{"role":"user","content":"你好"}])))),
            persona_manager=types.SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("p",{"prompt":"既有人设"},None,False))),
            get_current_chat_provider_id=AsyncMock(return_value="provider"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(role="assistant",completion_text="你好呀")))
        result=await AstrBotExpressionAdapter(context).generate(intent,"主动说一句")
        self.assertEqual(result,"你好呀")
        kwargs=context.llm_generate.call_args.kwargs
        self.assertTrue(kwargs["system_prompt"].startswith("既有人设"))
        self.assertEqual(kwargs["contexts"],[{"role":"user","content":"你好"}])
        self.assertNotIn("tools",kwargs)
        context.get_config=lambda **kw:{"provider_settings":{"enable":False}}
        with self.assertRaises(ValueError):await AstrBotExpressionAdapter(context).generate(intent,"x")


class ExpressionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.now=NOW
        cfg=HostSettings(qq_platform_ids=["qq"],qq_private_ids=["alice"],autonomous_enabled=False,
            expression=settings(send_enabled=True),simulation={"initial_needs":{"energy":100,"satiety":100,"boredom":0,"social_need":80},
            "initial_emotion":None,"activities":[]})
        self.generate=AsyncMock(return_value="最近怎么样？")
        self.send=AsyncMock(return_value=True)
        self.host=PersonaHost(cfg,Path(self.directory.name),clock=lambda:self.now,persist_controls=lambda policy:None,
            generate_expression=self.generate,send_expression=self.send)
        await self.host.start();await self.host.scheduler.stop()
        await self.host.set_controls(enabled=True)
        await self.host.store.conversation_receive(cfg.agent_id,incoming(),now=TIME,settings=cfg.conversation,directed=True)
        await self.host.store.record_event(incoming())
        result=await self.host.store.reserve_behavior(cfg.agent_id,self.host.selector,decision_key="worker",now=self.now,arbiter=BehaviorArbiter())
        self.item=result.expression
        self.assertIsNotNone(self.item)

    async def asyncTearDown(self):
        await self.host.stop();self.directory.cleanup()

    async def test_worker_success_generates_and_sends_once(self):
        await self.host._run_expression(self.item)
        await self.host._run_expression(self.item)
        self.generate.assert_awaited_once();self.send.assert_awaited_once()
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual(head.intent.status,"sent")
        context=await self.host.chat_context(TARGET.scope_id,speaker_id="qq:user:alice",platform_id="qq",user_id="alice")
        self.assertIn("最近怎么样",context.user_text)

    async def test_uncertain_send_is_not_retried(self):
        self.send.side_effect=OSError("uncertain")
        await self.host._run_expression(self.item)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual(head.intent.status,"unknown")
        await self.host._run_expression(self.item)
        self.send.assert_awaited_once()

    async def test_generation_failure_or_invalid_body_never_sends(self):
        self.generate.return_value=""
        await self.host._run_expression(self.item)
        self.send.assert_not_awaited()
        self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"failed")

    async def test_inbound_can_progress_during_generation_and_cancels_send(self):
        started=asyncio.Event();release=asyncio.Event()
        async def generate(*args):
            started.set();await release.wait();return "旧的问候"
        self.generate.side_effect=generate
        task=asyncio.create_task(self.host._run_expression(self.item))
        try:
            await asyncio.wait_for(started.wait(),2)
            event=incoming("new_actual",7200)
            await asyncio.wait_for(self.host.ingest(event,directed_to_agent=True),2)
            release.set();await task
            self.send.assert_not_awaited()
            self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"cancelled")
        finally:
            release.set()
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)

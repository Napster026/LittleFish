"""主动表达到期、人工控制和恢复边界；由开发者运行。"""

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock,patch

import test_expression as fixtures
import test_conversation as conversation_fixtures
from persona_agent.application.settings import HostSettings
from persona_agent.social.expression import ExpressionSettings
from test_astrbot_host import QQEvent
from persona_agent.activity.selection import ActivitySelector
from persona_agent.core.arbiter import BehaviorKind
from persona_agent.core.persistence import CorePersistence
from persona_agent.social.expression import gate,local_day
from persona_agent.social.expression_clock import expression_deadline_for,parse_expression_deadline
from test_autonomy import definition


class ExpressionControlStoreTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ExpressionStoreTests.asyncSetUp
    asyncTearDown=fixtures.ExpressionStoreTests.asyncTearDown
    reserve=fixtures.ExpressionStoreTests.reserve
    intent=fixtures.ExpressionStoreTests.intent
    claim=fixtures.ExpressionStoreTests.claim

    async def test_actual_intent_has_one_stable_deadline_preview_has_none(self):
        item=await self.intent();plan=expression_deadline_for(item)
        self.assertEqual(expression_deadline_for(item),plan)
        self.assertEqual(parse_expression_deadline(plan.event).intent_id,item.intent_id)
        self.assertEqual((await self.store.get_schedule("agent",plan.schedule_id)).schedule,plan)
        await self.reserve()
        self.assertEqual((await self.store.expression_head("agent")).used,1)

    async def test_preview_has_no_deadline_and_can_be_cancelled_without_budget_refund(self):
        self.store._expression=fixtures.settings()
        await self.reserve();head=await self.store.expression_head("agent")
        self.assertIsNone(await self.store.get_schedule("agent",expression_deadline_for(head.intent).schedule_id))
        changed,item=await self.store.cancel_expression("agent",now=self.now,include_preview=True)
        self.assertTrue(changed);self.assertEqual(item.status,"cancelled")
        self.assertEqual((await self.store.expression_head("agent")).used,1)

    async def test_deadline_creation_failure_rolls_back_intent_budget_and_audit(self):
        with patch.object(CorePersistence,"create_schedule",AsyncMock(side_effect=RuntimeError("deadline failed"))):
            with self.assertRaises(RuntimeError):await self.reserve()
        self.assertIsNone(await self.store.expression_head("agent"))

    async def test_expiry_releases_choice_and_replay_does_not_create_another_activity(self):
        self.selector=ActivitySelector((definition("social",social_need=-30),))
        item=await self.intent();event=expression_deadline_for(item).event
        self.now=item.expires_at
        changed,expired=await self.store.handle_expression_deadline("agent",event,now=self.now)
        self.assertTrue(changed);self.assertEqual(expired.status,"cancelled")
        first=await self.reserve(event.event_id)
        self.assertEqual(first.decision.winner.kind,BehaviorKind.START_ACTIVITY)
        self.assertEqual(await self.store.handle_expression_deadline("agent",event,now=self.now),(True,None))
        repeated=await self.reserve(event.event_id)
        self.assertEqual(repeated.activity.instance.activity_id,first.activity.instance.activity_id)
        self.assertEqual(len(await self.store.list_active_activities("agent")),1)

    async def test_expired_generating_cannot_claim_send(self):
        item=await self.intent();await self.claim(item)
        self.now=item.expires_at
        _,expired=await self.store.handle_expression_deadline("agent",expression_deadline_for(item).event,now=self.now)
        self.assertEqual(expired.reason,"intent_expired")
        self.assertIsNone(await self.claim(item,"send","迟到正文"))

    async def test_expired_inflight_is_unknown_and_late_result_cannot_reopen_conversation(self):
        item=await self.intent();await self.claim(item);await self.claim(item,"send","你好")
        self.now=item.expires_at
        _,expired=await self.store.handle_expression_deadline("agent",expression_deadline_for(item).event,now=self.now)
        self.assertEqual(expired.status,"unknown")
        self.assertIsNone(await self.store.finish_expression("agent",item.intent_id,now=self.now,status="sent"))
        conv=await self.store.get_conversation("agent",item.target.scope_id)
        self.assertNotEqual(conv.source_event_id,item.intent_id)

    async def test_future_forged_missing_plan_or_wrong_agent_deadline_is_rejected(self):
        item=await self.intent();plan=expression_deadline_for(item)
        with self.assertRaises(ValueError):await self.store.handle_expression_deadline("agent",plan.event,now=self.now)
        with self.assertRaises(ValueError):parse_expression_deadline(plan.event.model_copy(update={"scope_id":"other"}))
        self.now=item.expires_at
        with self.assertRaises(ValueError):await self.store.handle_expression_deadline("other",plan.event,now=self.now)
        async with self.store._backend.transaction() as session:
            await session.delete("scheduled_events",where={"schedule_id":plan.schedule_id})
        with self.assertRaises(ValueError):await self.store.handle_expression_deadline("agent",plan.event,now=self.now)

    async def test_obsolete_deadline_never_cancels_new_intent(self):
        old=await self.intent()
        await self.store.cancel_expression("agent",now=self.now)
        self.now+=timedelta(seconds=self.cfg.cooldown_seconds)
        new=(await self.reserve("new")).expression
        self.assertNotEqual(new.intent_id,old.intent_id)
        self.assertEqual(await self.store.handle_expression_deadline("agent",expression_deadline_for(old).event,now=self.now),(False,None))
        self.assertEqual((await self.store.expression_head("agent")).intent,new)

    async def test_legacy_expired_pending_gets_plan_and_resumes_through_scheduler(self):
        item=await self.intent();plan=expression_deadline_for(item)
        async with self.store._backend.transaction() as session:
            await session.delete("scheduled_events",where={"schedule_id":plan.schedule_id})
        self.now=item.expires_at
        self.assertIsNone(await self.store.recover_expression("agent",now=self.now))
        self.assertIsNotNone(await self.store.get_schedule("agent",plan.schedule_id))
        self.assertTrue((await self.store.handle_expression_deadline("agent",plan.event,now=self.now))[0])

    async def test_manual_cancel_is_idempotent_and_keeps_cooldown_and_budget(self):
        item=await self.intent();before=await self.store.expression_head("agent")
        self.assertTrue((await self.store.cancel_expression("agent",now=self.now))[0])
        self.assertFalse((await self.store.cancel_expression("agent",now=self.now))[0])
        after=await self.store.expression_head("agent")
        self.assertEqual((after.used,after.last_attempt_at),(before.used,before.last_attempt_at))
        self.assertEqual(gate(self.cfg,self.now,after),"cooldown")

    async def test_manual_cancel_never_claims_inflight_was_recalled(self):
        item=await self.intent();await self.claim(item);await self.claim(item,"send","你好")
        changed,current=await self.store.cancel_expression("agent",now=self.now)
        self.assertFalse(changed);self.assertEqual(current.status,"sending")

    async def test_disabled_startup_cancels_pending_without_resurrecting_on_resume(self):
        item=await self.intent()
        self.store._expression=self.cfg.model_copy(update={"enabled":False})
        self.assertIsNone(await self.store.recover_expression("agent",now=self.now))
        self.store._expression=self.cfg
        self.assertIsNone(await self.store.recover_expression("agent",now=self.now))
        self.assertEqual((await self.store.expression_head("agent")).intent.status,"cancelled")

    async def test_next_day_budget_resets_but_cross_day_cooldown_remains(self):
        item=await self.intent();await self.claim(item)
        await self.store.finish_expression("agent",item.intent_id,now=self.now,status="failed")
        head=await self.store.expression_head("agent")
        tomorrow=self.now+timedelta(hours=2)
        self.assertNotEqual(local_day(tomorrow,self.cfg),head.budget_day)
        self.assertEqual(gate(self.cfg,tomorrow,head),"cooldown")
        self.now+=timedelta(seconds=self.cfg.cooldown_seconds)
        await self.reserve("tomorrow")
        new=await self.store.expression_head("agent")
        self.assertEqual(new.used,1)
        self.assertEqual(new.budget_day,local_day(self.now,self.cfg))


class ExpressionControlWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ExpressionWorkerTests.asyncSetUp
    asyncTearDown=fixtures.ExpressionWorkerTests.asyncTearDown

    async def test_autonomy_pause_cancels_pending_immediately(self):
        await self.host.set_controls(enabled=False)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual(head.intent.reason,"autonomy_paused")
        self.assertEqual(head.intent.status,"cancelled")
        await self.host._run_expression(self.item)
        self.generate.assert_not_awaited();self.send.assert_not_awaited()

    async def test_expression_pause_resume_preserves_send_mode_and_never_reopens_cancelled(self):
        saved=[];self.host._persist_expression=saved.append
        await self.host.set_expression_enabled(False)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual(head.intent.reason,"expression_paused")
        await self.host.set_expression_enabled(True)
        self.assertTrue(self.host.settings.expression.send_enabled)
        self.assertEqual([cfg.enabled for cfg in saved],[False,True])
        self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"cancelled")
        self.generate.assert_not_awaited();self.send.assert_not_awaited()

    async def test_save_failure_keeps_settings_and_intent_unchanged(self):
        before=await self.host.store.expression_head(self.host.settings.agent_id)
        def fail(_):raise OSError("save failed")
        self.host._persist_expression=fail
        with self.assertRaises(OSError):await self.host.set_expression_enabled(False)
        self.assertTrue(self.host.settings.expression.enabled)
        self.assertEqual(await self.host.store.expression_head(self.host.settings.agent_id),before)

    async def test_queued_generation_can_be_cancelled_without_deadlock(self):
        started=asyncio.Event()
        async def generate(*args):started.set();await asyncio.Event().wait()
        self.generate.side_effect=generate
        self.host._queue_expression(self.item)
        await asyncio.wait_for(started.wait(),2)
        message=await asyncio.wait_for(self.host.cancel_expression(),2)
        await asyncio.gather(self.host._expression_task,return_exceptions=True)
        self.assertIn("已取消",message)
        self.send.assert_not_awaited()
        self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.reason,"manual_cancel")

    async def test_pause_during_send_keeps_real_result_and_does_not_claim_recall(self):
        started=asyncio.Event();release=asyncio.Event()
        async def send(*args):started.set();await release.wait();return True
        self.send.side_effect=send
        self.host._queue_expression(self.item)
        try:
            await asyncio.wait_for(started.wait(),2)
            message=await self.host.cancel_expression()
            self.assertIn("不能确认撤回",message)
            await self.host.set_controls(enabled=False)
            self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"sending")
            release.set();await self.host._expression_task
            self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"sent")
        finally:
            release.set()

    async def test_generation_and_send_timeout_have_distinct_status(self):
        self.generate.side_effect=asyncio.TimeoutError()
        await self.host._run_expression(self.item)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual((head.intent.status,head.intent.reason),("failed","generation_timeout"))
        self.send.assert_not_awaited()

    async def test_send_timeout_stays_unknown_and_never_retries(self):
        self.send.side_effect=asyncio.TimeoutError()
        await self.host._run_expression(self.item)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual((head.intent.status,head.intent.reason),("unknown","send_timeout"))
        await self.host._run_expression(self.item)
        self.send.assert_awaited_once()

    async def test_next_intent_waits_for_cancelled_worker_then_dispatches(self):
        started=asyncio.Event();cleanup=asyncio.Event();release=asyncio.Event();sent=asyncio.Event()
        cfg=self.host.settings.expression.model_copy(update={"cooldown_seconds":60})
        self.host.settings=self.host.settings.model_copy(update={"expression":cfg})
        self.host.store._expression=cfg
        calls=0
        async def generate(*args):
            nonlocal calls
            calls+=1
            if calls==1:
                started.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup.set();await release.wait();raise
            return "新的问候"
        async def send(*args):sent.set();return True
        self.generate.side_effect=generate;self.send.side_effect=send
        self.host._queue_expression(self.item)
        old_task=self.host._expression_task
        try:
            await asyncio.wait_for(started.wait(),2)
            self.now=self.item.expires_at
            await self.host._deliver(expression_deadline_for(self.item).event)
            await asyncio.wait_for(cleanup.wait(),2)
            next_intent=(await self.host.store.expression_head(self.host.settings.agent_id)).intent
            self.assertNotEqual(next_intent.intent_id,self.item.intent_id)
            self.assertEqual(next_intent.status,"pending")
            release.set();await asyncio.gather(old_task,return_exceptions=True)
            await asyncio.wait_for(sent.wait(),2)
            await self.host._expression_task
            self.assertEqual((await self.host.store.expression_head(self.host.settings.agent_id)).intent.status,"sent")
            self.send.assert_awaited_once()
        finally:
            release.set()

    async def test_generation_failure_reconsiders_without_waiting_for_need_tick(self):
        self.generate.return_value=""
        self.host.autonomy.consider=AsyncMock()
        await self.host._run_expression(self.item)
        self.host.autonomy.consider.assert_awaited_once_with("expression-finished:"+self.item.intent_id+":failed")

    async def test_restart_interrupted_generation_uses_fresh_decision_key(self):
        from persona_agent.application.host import PersonaHost
        from persona_agent.application.autonomy import selection_id
        cfg=self.host.settings.model_copy(update={"autonomous_enabled":True})
        data_dir=self.host.data_dir
        await self.host.store.claim_expression(cfg.agent_id,self.item.intent_id,now=self.now,autonomous_enabled=True)
        await self.host.stop()
        self.host=PersonaHost(cfg,data_dir,clock=lambda:self.now,persist_controls=lambda policy:None,
            generate_expression=self.generate,send_expression=self.send)
        await self.host.start();await self.host.scheduler.stop()
        key="expression-recovery:"+self.item.intent_id+":failed"
        self.assertIsNotNone(await self.host.store.get_event(selection_id(cfg.agent_id,key)))
        self.assertEqual((await self.host.store.expression_head(cfg.agent_id)).intent.status,"failed")
        self.generate.assert_not_awaited();self.send.assert_not_awaited()

    async def test_host_deadline_delivery_releases_pending(self):
        self.now=self.item.expires_at
        await self.host._deliver(expression_deadline_for(self.item).event)
        head=await self.host.store.expression_head(self.host.settings.agent_id)
        self.assertEqual((head.intent.status,head.intent.reason),("cancelled","intent_expired"))
        self.assertNotEqual(self.host.autonomy.last_decision.reason,"expression_pending")
        self.generate.assert_not_awaited()


class ExpressionCommandTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=conversation_fixtures.PluginConversationTests.asyncSetUp

    async def test_control_commands_keep_admin_private_and_binding_gates(self):
        self.plugin.host.expression_status=AsyncMock(return_value="只读诊断")
        self.plugin.host.cancel_expression=AsyncMock(return_value="已取消")
        self.plugin.host.set_expression_enabled=AsyncMock()
        self.assertIsNone(await self.plugin._command(QQEvent(admin=False),"expression","cancel"))
        self.assertIn("管理员私聊",await self.plugin._command(QQEvent(),"expression","cancel"))
        self.plugin.host.cancel_expression.assert_not_awaited()
        event=QQEvent(group="")
        self.assertIn("管理员私聊",await self.plugin._command(event,"expression","cancel"))
        self.plugin.host.settings=HostSettings(qq_private_ids=["sender-1"])
        self.assertEqual(await self.plugin._command(event,"expression","show"),"只读诊断")
        self.assertEqual(await self.plugin._command(event,"expression","cancel"),"已取消")
        await self.plugin._command(event,"expression","pause")
        self.plugin.host.set_expression_enabled.assert_awaited_once_with(False)
        self.assertIn("用法",await self.plugin._command(event,"expression","retry"))

    async def test_config_save_failure_restores_existing_or_missing_expression_field(self):
        class Config(dict):
            def save_config(self):raise OSError("save failed")
        for values in ({},{"expression":{"enabled":True,"send_enabled":False}}):
            self.plugin.config=Config(values)
            with self.assertRaises(OSError):self.plugin._save_expression(ExpressionSettings(enabled=False))
            self.assertEqual(self.plugin.config,values)

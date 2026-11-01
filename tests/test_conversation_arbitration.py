"""会话占用、到期调度与自主行为的集成回归；由开发者运行。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from persona_agent.activity.models import ActivityStatus, ExecutionMode
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.service import ActivityService
from persona_agent.activity.selection import ActivitySelector
from persona_agent.application.autonomy import AutonomousActivityService
from persona_agent.application.persistence import PersonaStore
from persona_agent.application.settings import HostSettings
from persona_agent.application.host import PersonaHost
from persona_agent.application.conversation import conversation_scope_allowed
from persona_agent.core.arbiter import BehaviorArbiter, BehaviorKind
from persona_agent.core.events import AgentEvent
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.needs import NeedState
from persona_agent.social.conversation import ConversationSettings, conversation_occupancy
from persona_agent.social.conversation_clock import deadline_for, parse_conversation_deadline
from test_conversation import TIME, incoming
from test_autonomy import definition


class ConversationArbitrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.path=Path(self.directory.name)/"persona.sqlite3"
        self.now=TIME
        self.settings=ConversationSettings()
        self.store=PersonaStore(SQLiteStorage(self.path),conversation=self.settings)
        await self.store.open()
        self.agent="agent"
        self.state=AgentState(agent_id=self.agent,updated_at=TIME,
            needs=NeedState(energy=20,satiety=100,boredom=0,social_need=0))
        await self.store.create_state(self.state)
        self.selector=ActivitySelector((definition(),))
        registry=ExecutorRegistry()
        registry.register("rest",ExecutionMode.SIMULATED,SimulatedActivityExecutor())
        self.activities=ActivityService(self.agent,self.store,ActivityManager(registry),clock=lambda:self.now)
        self.runtime=AgentRuntime(self.agent,self.store,EventRouter(),clock=lambda:self.now)
        await self.runtime.start()
        self.service=AutonomousActivityService(self.agent,self.store,self.selector,self.activities,self.runtime,
            clock=lambda:self.now)

    async def asyncTearDown(self):
        await self.runtime.stop()
        await self.store.close()
        self.directory.cleanup()

    async def receive(self,event=None,directed=True):
        event=event or incoming()
        await self.store.conversation_receive(self.agent,event,now=self.now,settings=self.settings,directed=directed)
        return await self.store.get_conversation(self.agent,event.scope_id)

    async def sent(self,event=None):
        event=event or incoming()
        await self.store.conversation_sent(self.agent,event,now=self.now,settings=self.settings)
        return await self.store.get_conversation(self.agent,event.scope_id)

    async def reserve(self,key):
        return await self.store.reserve_behavior(self.agent,self.selector,decision_key=key,now=self.now,arbiter=BehaviorArbiter())

    async def test_pending_reply_and_waiting_block_and_expiry_releases(self):
        await self.receive()
        blocked=await self.reserve("pending")
        self.assertEqual(blocked.decision.reason,"reply_pending")
        self.assertEqual(await self.store.list_active_activities(self.agent),())
        await self.sent()
        waiting=await self.reserve("waiting")
        self.assertEqual(waiting.decision.reason,"waiting_followup")
        self.now=TIME+timedelta(seconds=120)
        ready=await self.reserve("expired")
        self.assertEqual(ready.decision.winner.kind,BehaviorKind.START_ACTIVITY)
        self.assertEqual(await self.store.get_state(self.agent),self.state)
        # 原决策回执不因时间经过而改写；到期使用新的决策键。
        self.assertEqual((await self.reserve("pending")).decision,blocked.decision)

    async def test_end_and_budget_exhaustion_release_occupancy(self):
        await self.receive();await self.sent()
        self.now+=timedelta(seconds=1)
        bye=incoming("bye",1)
        bye=AgentEvent(**{**bye.model_dump(),"payload":{**bye.payload,"text":"先聊到这"}})
        await self.receive(bye,directed=False)
        self.assertIsNone(await self.store.conversation_arbitration_reason(self.agent,now=self.now))
        self.assertEqual((await self.reserve("closed")).decision.winner.kind,BehaviorKind.START_ACTIVITY)

    async def test_last_automatic_reply_does_not_hold_full_window(self):
        await self.receive();await self.sent()
        for i in range(1,4):
            self.now=TIME+timedelta(seconds=i)
            event=incoming(str(i),i)
            state=await self.receive(event,directed=False)
            self.assertEqual(conversation_occupancy(state,self.now,self.settings),"reply_pending")
            state=await self.sent(event)
        self.assertIsNone(conversation_occupancy(state,self.now,self.settings))
        self.assertEqual((await self.reserve("budget_end")).decision.winner.kind,BehaviorKind.START_ACTIVITY)

    async def test_pending_reservation_rechecked_but_running_keeps_same_instance(self):
        original=await self.reserve("original")
        await self.receive()
        blocked=await self.reserve("original")
        self.assertIsNone(blocked.activity)
        self.assertEqual(blocked.decision.reason,"reply_pending")
        self.now=TIME+timedelta(seconds=120)
        recovered=await self.reserve("released")
        self.assertEqual(recovered.activity.instance.activity_id,original.activity.instance.activity_id)
        self.assertEqual(recovered.decision.reason,"recover_pending")
        await self.activities.start_activity(recovered.activity.instance.activity_id)
        self.now+=timedelta(seconds=1)
        await self.receive(incoming("new",121))
        decision=await self.reserve("running")
        self.assertEqual(decision.decision.winner.kind,BehaviorKind.CONTINUE_ACTIVITY)
        self.assertEqual(decision.decision.conversation_reason,"reply_pending")
        self.assertEqual(decision.activity.instance.activity_id,original.activity.instance.activity_id)
        self.assertEqual(decision.activity.instance.status,ActivityStatus.RUNNING)

    async def test_disabled_deferral_keeps_chat_without_blocking_or_plans(self):
        self.store._conversation=ConversationSettings(defer_new_activities=False)
        await self.receive()
        self.assertIsNone(await self.store.conversation_arbitration_reason(self.agent,now=self.now))
        self.assertEqual((await self.reserve("allowed")).decision.winner.kind,BehaviorKind.START_ACTIVITY)
        self.assertEqual(await self.store.list_due_schedules(self.agent,now=TIME+timedelta(days=1)),())

    async def test_multiple_scopes_waiting_cannot_mask_pending(self):
        await self.receive();await self.sent()
        second=incoming("another",scope="qq:FriendMessage:bob",user="bob")
        await self.receive(second)
        reason=await self.store.conversation_arbitration_reason(self.agent,now=self.now)
        self.assertEqual(reason,"reply_pending")
        await self.sent(second)
        reason=await self.store.conversation_arbitration_reason(self.agent,now=self.now)
        self.assertEqual(reason,"waiting_followup")

    async def test_stale_deadline_and_other_scope_still_active(self):
        old=await self.receive()
        self.now+=timedelta(seconds=1)
        current=await self.sent()
        self.now=old.expires_at
        self.assertFalse(await self.store.handle_conversation_deadline(self.agent,deadline_for(old).event,now=self.now))
        self.assertEqual(await self.store.conversation_arbitration_reason(self.agent,now=self.now),"waiting_followup")
        second=incoming("other",120,scope="qq:FriendMessage:bob",user="bob")
        await self.receive(second)
        self.now=current.expires_at
        event=deadline_for(current).event
        self.assertTrue(await self.store.handle_conversation_deadline(self.agent,event,now=self.now))
        result=await self.reserve(event.event_id)
        self.assertEqual(result.decision.reason,"reply_pending")

    async def test_deadline_validation_and_replay_does_not_double_start(self):
        state=await self.receive()
        plan=deadline_for(state)
        self.assertEqual(deadline_for(state),plan)
        self.assertEqual(parse_conversation_deadline(plan.event).revision,state.revision)
        with self.assertRaises(ValueError):
            await self.store.handle_conversation_deadline(self.agent,plan.event,now=self.now)
        with self.assertRaises(ValueError):
            parse_conversation_deadline(plan.event.model_copy(update={"scope_id":"wrong"}))
        self.now=state.expires_at
        with self.assertRaises(ValueError):
            await self.store.handle_conversation_deadline("other",plan.event,now=self.now)
        for _ in range(2):
            self.assertTrue(await self.store.handle_conversation_deadline(self.agent,plan.event,now=self.now))
            await self.service.consider(plan.event.event_id)
        self.assertEqual(len(await self.store.list_active_activities(self.agent)),1)
        self.assertEqual(await self.store.get_state(self.agent),self.state)

    async def test_schedule_and_cursor_rollback_together(self):
        with patch("persona_agent.core.persistence.CorePersistence.create_schedule",AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.receive()
        self.assertIsNone(await self.store.get_conversation(self.agent,incoming().scope_id))

    async def test_missing_persisted_deadline_and_disabled_plan_are_rejected_or_ignored(self):
        self.store._conversation=None
        state=await self.receive()
        self.store._conversation=self.settings
        self.now=state.expires_at
        with self.assertRaises(ValueError):
            await self.store.handle_conversation_deadline(self.agent,deadline_for(state).event,now=self.now)
        self.now=TIME
        await self.store.start_conversation_clock(self.agent,now=self.now)
        self.store._conversation=ConversationSettings(enabled=False)
        self.now=state.expires_at
        self.assertFalse(await self.store.handle_conversation_deadline(self.agent,deadline_for(state).event,now=self.now))

    async def test_restart_bootstrap_legacy_cursor_without_resetting_deadline(self):
        self.store._conversation=None
        state=await self.receive()
        self.store._conversation=self.settings
        await self.store.close();await self.store.open()
        self.now+=timedelta(seconds=10)
        await self.store.start_conversation_clock(self.agent,now=self.now)
        self.assertEqual(await self.store.get_conversation(self.agent,state.scope_id),state)
        self.assertIsNotNone(await self.store.get_schedule(self.agent,deadline_for(state).schedule_id))
        self.assertEqual((await self.reserve("startup")).decision.reason,"reply_pending")

    async def test_only_current_bound_scopes_can_block(self):
        settings=HostSettings(qq_private_ids=["alice"],qq_platform_ids=["qq"])
        self.store._conversation_scope_allowed=lambda item:conversation_scope_allowed(item,settings)
        state=await self.receive()
        self.assertEqual(await self.store.conversation_arbitration_reason(self.agent,now=self.now),"reply_pending")
        settings=HostSettings(qq_private_ids=["bob"],qq_platform_ids=["qq"])
        self.assertIsNone(await self.store.conversation_arbitration_reason(self.agent,now=self.now))
        self.now=state.expires_at
        self.assertFalse(await self.store.handle_conversation_deadline(self.agent,deadline_for(state).event,now=self.now))

    async def test_bad_row_and_bounded_paging_do_not_block_healthy_conversation(self):
        async with self.store._backend.transaction() as session:
            for i in range(101):
                await session.insert("conversations",{"agent_id":self.agent,"scope_id":f"a{i:03}",
                    "revision":1,"state_json":"broken"})
        await self.receive()
        async with self.store._backend.transaction() as session:
            kind=type(session)
        original=kind.fetch_all
        calls=[]
        async def guarded(session,table,**kwargs):
            if table=="conversations":
                self.assertEqual(kwargs["where"],{"agent_id":self.agent})
                self.assertEqual(kwargs["limit"],100)
                calls.append(kwargs.get("after"))
            elif table=="agent_events":
                self.fail("arbitration must not scan event history")
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=guarded):
            self.assertEqual(await self.store.conversation_arbitration_reason(self.agent,now=self.now),"reply_pending")
        self.assertEqual(len(calls),2)
        self.assertIsNotNone(calls[1])


class HostConversationArbitrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_transitions_diagnostics_and_deadline_delivery(self):
        with TemporaryDirectory() as directory:
            now=TIME
            settings=HostSettings(qq_private_ids=["alice"],qq_platform_ids=["qq"],autonomous_enabled=False)
            host=PersonaHost(settings,Path(directory),clock=lambda:now)
            await host.start()
            await host.scheduler.stop()
            try:
                # 更换策略需要持久化回调，测试中显式提供。
                host._persist_controls=lambda policy:None
                await host.set_controls(enabled=True)
                await host.conversation_receive(incoming(),directed=True)
                self.assertEqual(host.autonomy.last_decision.reason,"reply_pending")
                self.assertIn("正在准备回复",await host.decision())
                await host.conversation_sent(incoming())
                self.assertEqual(host.autonomy.last_decision.reason,"waiting_followup")
                item=await host.store.get_conversation(settings.agent_id,incoming().scope_id)
                now=item.expires_at
                await host._deliver(deadline_for(item).event)
                self.assertEqual(host.autonomy.last_decision.winner.kind,BehaviorKind.START_ACTIVITY)
                self.assertIn("当前会话约束：无",await host.decision())
                self.assertEqual(len(await host.store.list_active_activities(settings.agent_id)),1)
            finally:
                await host.stop()

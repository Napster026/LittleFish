"""续聊边界与持久化回归测试；由开发者运行。"""

import importlib.util
import logging
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.events import AgentEvent
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.needs import NeedState
from persona_agent.social.conversation import ConversationSettings, can_continue, ends_conversation
from test_astrbot_host import QQEvent
from schema_fixtures import restore_v7_layout

TIME=datetime(2026,11,1,12,tzinfo=timezone.utc)
ROOT=Path(__file__).resolve().parents[1]


def incoming(key="one",seconds=0,user="alice",scope="qq:FriendMessage:alice",group="",platform="qq"):
    return AgentEvent(event_id=key,event_type="SOCIAL.message_received",timestamp=TIME+timedelta(seconds=seconds),
        source="astrbot.aiocqhttp",provenance="observed",scope_id=scope,
        payload={"sender_id":user,"platform_id":platform,"group_id":group,"text":"继续说吧"})


class ConversationRules(unittest.TestCase):
    def test_settings_strict_and_end_phrase_is_whole_sentence(self):
        for args in ({"enabled":"true"},{"max_followups":0},{"window_seconds":1},{"end_phrases":[""]}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                ConversationSettings(**args)
        self.assertTrue(ends_conversation("先聊到这！",ConversationSettings()))
        self.assertFalse(ends_conversation("他说先聊到这是什么意思",ConversationSettings()))
        self.assertFalse(ends_conversation("不要再见",ConversationSettings()))


class ConversationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.path=Path(self.directory.name)/"persona.sqlite3"
        self.store=PersonaStore(SQLiteStorage(self.path))
        await self.store.open()
        self.state=AgentState(agent_id="agent",needs=NeedState(energy=50,satiety=50,boredom=50,social_need=50),updated_at=TIME)
        await self.store.create_state(self.state)
        self.settings=ConversationSettings()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def receive(self,event,*,directed=False,now=None,blocked=False):
        return await self.store.conversation_receive("agent",event,now=now or event.timestamp,
            settings=self.settings,directed=directed,blocked=blocked)

    async def sent(self,event,seconds=0):
        await self.store.conversation_sent("agent",event,now=TIME+timedelta(seconds=seconds),settings=self.settings)

    async def opened(self):
        event=incoming()
        self.assertFalse(await self.receive(event,directed=True))
        await self.sent(event)
        return event

    async def test_only_send_hook_opens_window_and_replay_does_not_extend(self):
        first=incoming()
        await self.receive(first,directed=True)
        self.assertFalse(await self.receive(incoming("two",1)))
        await self.sent(first)
        old=await self.store.get_conversation("agent",first.scope_id)
        await self.sent(first,seconds=5)
        self.assertEqual(await self.store.get_conversation("agent",first.scope_id),old)
        self.assertTrue(await self.receive(incoming("two",1)))
        self.assertFalse(await self.receive(incoming("two",1)))
        self.assertEqual(await self.store.get_state("agent"),self.state)

    async def test_followup_budget_and_normal_wake_resets_it(self):
        await self.opened()
        for i in range(1,4):
            event=incoming(str(i),i)
            self.assertTrue(await self.receive(event))
            await self.sent(event,seconds=i)
        self.assertFalse(await self.receive(incoming("limit",4)))
        reset=incoming("normal",5)
        self.assertFalse(await self.receive(reset,directed=True))
        await self.sent(reset,seconds=5)
        self.assertTrue(await self.receive(incoming("again",6)))

    async def test_scope_user_platform_and_time_bounds(self):
        await self.opened()
        for item in (incoming("other",1,user="bob"),incoming("scope",1,scope="qq:other"),
                incoming("platform",1,platform="other"),incoming("old",-1),incoming("expired",120)):
            with self.subTest(item=item.event_id):
                self.assertFalse(await self.receive(item))
        self.assertFalse(await self.receive(incoming("late",1),now=TIME+timedelta(seconds=40)))
        self.assertFalse(await self.receive(incoming("future",2),now=TIME+timedelta(seconds=1)))

    async def test_private_default_and_blocked_messages(self):
        await self.opened()
        self.assertFalse(await self.receive(incoming("blocked",1),blocked=True))
        group=incoming("group",1,scope="qq:GroupMessage:g",group="g")
        self.assertFalse(await self.receive(group,directed=True))
        self.settings=ConversationSettings(private_only=False)
        await self.receive(group,directed=True)
        await self.sent(group,seconds=1)
        self.assertTrue(await self.receive(incoming("group2",2,scope=group.scope_id,group="g")))

    async def test_end_sentence_closes_and_send_does_not_reopen(self):
        await self.opened()
        event=incoming("bye",1)
        event=event.model_copy(update={"payload":{**event.payload,"text":"先聊到这"}})
        self.assertFalse(await self.receive(event))
        await self.sent(event,seconds=2)
        self.assertEqual((await self.store.get_conversation("agent",event.scope_id)).phase,"closed")
        self.assertFalse(await self.receive(incoming("again",3)))

    async def test_old_callback_cannot_steal_new_target(self):
        first=await self.opened()
        second=incoming("bob",1,user="bob")
        await self.receive(second,directed=True)
        await self.sent(first,seconds=2)
        state=await self.store.get_conversation("agent",first.scope_id)
        self.assertEqual(state.user_id,"bob")
        self.assertEqual(state.phase,"awaiting_reply")

    async def test_callback_identity_and_disabled_policy_do_not_open(self):
        first=incoming()
        await self.receive(first,directed=True)
        await self.sent(first.model_copy(update={"payload":{**first.payload,"sender_id":"bob"}}),seconds=1)
        self.assertEqual((await self.store.get_conversation("agent",first.scope_id)).phase,"awaiting_reply")
        self.settings=ConversationSettings(enabled=False)
        await self.sent(first,seconds=1)
        self.assertFalse(await self.receive(incoming("next",2)))
        self.assertEqual((await self.store.get_conversation("agent",first.scope_id)).phase,"awaiting_reply")

    async def test_restart_keeps_deadline_and_does_not_resend(self):
        first=await self.opened()
        old=await self.store.get_conversation("agent",first.scope_id)
        await self.store.close()
        self.store=PersonaStore(SQLiteStorage(self.path))
        await self.store.open()
        self.assertEqual(await self.store.get_conversation("agent",first.scope_id),old)
        self.assertTrue(await self.receive(incoming("after_restart",10)))
        await self.store.close()
        await self.store.open()
        self.assertFalse(await self.receive(incoming("another",11)))

    async def test_recorded_raw_event_is_not_reactivated(self):
        await self.opened()
        event=incoming("replay",1)
        await self.store.record_event(event)
        self.assertFalse(await self.receive(event))

    async def test_v7_upgrade_preserves_state_and_failed_migration_rolls_back(self):
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            restore_v7_layout(db)
            db.execute("CREATE VIEW conversations AS SELECT 1")
            db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            await self.store.open()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],7)
            db.execute("DROP VIEW conversations")
            db.commit()
        await self.store.open()
        self.assertEqual(await self.store.get_state("agent"),self.state)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],9)


class PluginConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        def decorator(*args,**kwargs): return lambda fn: fn
        class Star:
            def __init__(self,context): self.logger=logging.getLogger("test.conversation")
        api=types.ModuleType("astrbot.api");api.AstrBotConfig=dict
        events=types.ModuleType("astrbot.api.event");events.AstrMessageEvent=QQEvent
        events.filter=types.SimpleNamespace(command=decorator,permission_type=decorator,
            on_llm_request=decorator,after_message_sent=decorator,custom_filter=decorator,
            CustomFilter=object,PermissionType=types.SimpleNamespace(ADMIN=1))
        star=types.ModuleType("astrbot.api.star");star.Star=Star;star.Context=object;star.StarTools=object
        provider=types.ModuleType("astrbot.api.provider");provider.ProviderRequest=object
        package=types.ModuleType("conversation_plugin_test");package.__path__=[str(ROOT)]
        spec=importlib.util.spec_from_file_location("conversation_plugin_test.main",ROOT/"main.py")
        module=importlib.util.module_from_spec(spec)
        self.patcher=patch.dict(sys.modules,{"astrbot":types.ModuleType("astrbot"),"astrbot.api":api,
            "astrbot.api.event":events,"astrbot.api.star":star,"astrbot.api.provider":provider,
            "conversation_plugin_test":package,spec.name:module})
        self.patcher.start();self.addCleanup(self.patcher.stop)
        spec.loader.exec_module(module)
        from conversation_plugin_test.persona_agent.application.settings import HostSettings
        self.plugin=module.PersonaPlugin(object(),{})
        self.plugin.host=types.SimpleNamespace(ready=True,settings=HostSettings(qq_group_ids=["group-1"]),
            conversation_receive=AsyncMock(return_value=True),ingest=AsyncMock(),conversation_sent=AsyncMock())

    async def test_followup_only_marks_default_pipeline_no_extra_model_call(self):
        event=QQEvent();event.is_at_or_wake_command=False
        await self.plugin.receive_message(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(event.get_extra("persona_followup"))
        self.plugin.host.ingest.assert_awaited_once()
        self.assertTrue(self.plugin.host.ingest.call_args.kwargs["directed_to_agent"])
        await self.plugin.receive_message(event)
        self.plugin.host.conversation_receive.assert_awaited_once()

    async def test_group_foreign_target_commands_and_empty_are_blocked(self):
        for name,fields in (("At",{"qq":"other"}),("Reply",{"sender_id":"other"}),("AtAll",{})):
            event=QQEvent();event.message_obj.message=[type(name,(),fields)()]
            self.assertTrue(self.plugin._conversation_blocked(event))
        self.assertTrue(self.plugin._conversation_blocked(QQEvent(text="/other")))
        self.assertTrue(self.plugin._conversation_blocked(QQEvent(text="  ")))
        event=QQEvent();event.set_extra("handlers_parsed_params",{"other":{}})
        self.assertTrue(self.plugin._conversation_blocked(event))

    async def test_send_hook_requires_model_nonempty_result_and_send_observation(self):
        event=QQEvent();event.get_result=lambda:types.SimpleNamespace(is_llm_result=lambda:True,chain=["reply"])
        await self.plugin.observe_chat_sent(event)
        self.plugin.host.conversation_sent.assert_not_awaited()
        event.set_extra("persona_model_requested",True)
        event._has_send_oper=True
        await self.plugin.observe_chat_sent(event)
        self.plugin.host.conversation_sent.assert_awaited_once()
        self.plugin.host.conversation_sent.reset_mock()
        event.get_result=lambda:types.SimpleNamespace(is_llm_result=lambda:False,chain=["stream"])
        await self.plugin.observe_chat_sent(event)
        self.plugin.host.conversation_sent.assert_not_awaited()

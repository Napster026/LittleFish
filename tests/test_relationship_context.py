"""关系只读上下文和宿主诊断；由用户手动运行，不访问模型或 QQ。"""

import json
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from persona_agent.application.host import PersonaHost
from persona_agent.application.settings import HostSettings
from persona_agent.application.chat_context import apply_prompt_context
from persona_agent.cognition.prompt_builder import (ChatContextSettings, ChatSnapshot, ContextRelationship,
    ContextActivity, build_prompt_context)
from persona_agent.core.events import AgentEvent
from persona_agent.persona.needs import NeedState
from persona_agent.social.persistence import RelationshipPersistence

TIME=datetime(2026,11,1,12,tzinfo=timezone.utc)
SCOPE="LittleFish:FriendMessage:alice"
ACTOR="LittleFish:user:alice"


def message(identifier="one",scope_id=SCOPE,user_id="alice",group_id=""):
    return AgentEvent(event_id="qq:"+identifier,event_type="SOCIAL.message_received",timestamp=TIME,
        source="astrbot.aiocqhttp",provenance="observed",scope_id=scope_id,
        payload={"platform_id":"LittleFish","sender_id":user_id,"group_id":group_id,"bot_id":"bot","text":"谢谢你"})


class RelationshipPromptTests(unittest.TestCase):
    def snapshot(self,**changes):
        relationship=ContextRelationship(agent_id="persona-001",actor_id=ACTOR,familiarity=5,affinity=-.5,
            last_interaction_at=TIME,updated_at=TIME)
        return ChatSnapshot(**{**dict(agent_id="persona-001",scope_id=SCOPE,speaker_id=ACTOR,state_version=1,as_of=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50),emotion=None,relationship=relationship),**changes})

    def test_matching_actor_gets_only_numeric_estimate_as_data(self):
        context=build_prompt_context(self.snapshot(),ChatContextSettings())
        data=json.loads(context.user_text.removeprefix("persona_state="))
        self.assertEqual(data["relationship"]["with"],ACTOR)
        self.assertEqual(data["relationship"]["basis"],"interaction_estimate")
        self.assertEqual(data["relationship"]["ranges"],{"familiarity":[0,100],"affinity":[-100,100]})
        self.assertEqual(data["relationship"]["familiarity"],5)
        self.assertEqual(data["relationship"]["affinity"],-.5)
        self.assertEqual(context.relationship_actor_id,ACTOR)
        self.assertLessEqual(context.char_count,1000)

    def test_wrong_actor_agent_missing_identity_or_future_profile_is_not_injected(self):
        base=self.snapshot()
        variants=({"speaker_id":None},{"speaker_id":"LittleFish:user:bob"},{"agent_id":"other"},
            {"relationship":ContextRelationship(**{**base.relationship.model_dump(),"updated_at":TIME+timedelta(seconds=1)})})
        for changes in variants:
            with self.subTest(changes=changes):
                context=build_prompt_context(self.snapshot(**changes),ChatContextSettings())
                self.assertIsNone(context.relationship_actor_id)
                self.assertNotIn('"relationship":',context.user_text)

    def test_small_budget_drops_optional_relation_and_keeps_identity_metadata_honest(self):
        long_actor="platform:user:"+"a"*400
        profile=ContextRelationship(agent_id="persona-001",actor_id=long_actor,familiarity=5,affinity=1,
            last_interaction_at=TIME,updated_at=TIME)
        context=build_prompt_context(self.snapshot(speaker_id=long_actor,relationship=profile,
            activities=(ContextActivity(label="休息",mode="simulated",status="running",progress=.2),)),
            ChatContextSettings(max_chars=500))
        self.assertLessEqual(context.char_count,500)
        self.assertTrue(context.truncated)
        self.assertIsNone(context.relationship_actor_id)
        json.loads(context.user_text.removeprefix("persona_state="))

    def test_request_refresh_removes_previous_users_relation_and_preserves_other_content(self):
        request=types.SimpleNamespace(system_prompt="原有人设",extra_user_content_parts=[{"type":"text","text":"其他内容"}])
        alice=build_prompt_context(self.snapshot(),ChatContextSettings())
        bob=build_prompt_context(self.snapshot(speaker_id="LittleFish:user:bob",relationship=None),ChatContextSettings())
        apply_prompt_context(request,alice)
        self.assertIn(ACTOR,request.extra_user_content_parts[-1]["text"])
        apply_prompt_context(request,bob)
        self.assertNotIn(ACTOR,request.extra_user_content_parts[-1]["text"])
        self.assertEqual(request.extra_user_content_parts[0]["text"],"其他内容")


class RelationshipHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.now=TIME
        self.host=PersonaHost(HostSettings(),Path(self.directory.name),clock=lambda:self.now)
        await self.host.start()
        await self.host.scheduler.stop()

    async def asyncTearDown(self):
        await self.host.stop()
        self.directory.cleanup()

    async def context(self,**changes):
        return await self.host.chat_context(SCOPE,**{**dict(speaker_id=ACTOR,platform_id="LittleFish",user_id="alice"),**changes})

    async def test_ingest_then_context_and_show_are_read_only_and_do_not_count_commands(self):
        source=message()
        await self.host.ingest(source)
        profile=await self.host.store.get_relationship("persona-001","LittleFish","alice")
        state=await self.host.store.get_state("persona-001")
        context=await self.context()
        self.assertEqual(context.relationship_actor_id,ACTOR)
        self.assertIn("熟悉度",await self.host.relationship_command(source,"show"))
        self.assertIn(source.event_id,await self.host.relationship_command(source,"history"))
        self.assertEqual(await self.host.store.get_relationship("persona-001","LittleFish","alice"),profile)
        self.assertEqual(await self.host.store.get_state("persona-001"),state)
        async with self.host.store._repositories() as (core,_):
            self.assertEqual(len(await core.session.fetch_all("relationship_receipts")),1)

    async def test_read_requires_explicit_platform_user_and_matching_speaker(self):
        await self.host.ingest(message())
        with patch.object(RelationshipPersistence,"get",side_effect=AssertionError("identity mismatch queried relation")):
            context=await self.context(speaker_id="LittleFish:user:bob")
            self.assertIsNone(context.relationship_actor_id)
            context=await self.context(platform_id=None,user_id=None)
            self.assertIsNone(context.relationship_actor_id)
        context=await self.context(user_id="bob",speaker_id="LittleFish:user:bob")
        self.assertIsNone(context.relationship_actor_id)

    async def test_private_default_and_feature_switch_hide_relation_without_deleting_it(self):
        await self.host.ingest(message())
        self.assertIsNone((await self.context(is_private=False)).relationship_actor_id)
        group=message("group",scope_id="LittleFish:GroupMessage:g",group_id="g")
        self.assertIn("仅在绑定私聊",await self.host.relationship_command(group))
        changed=self.host.settings.model_dump()
        changed["relationships"]["enabled"]=False
        self.host.settings=HostSettings(**changed)
        self.assertIsNone((await self.context()).relationship_actor_id)
        self.assertIn("已关闭",await self.host.relationship_command(message()))
        self.assertIsNotNone(await self.host.store.get_relationship("persona-001","LittleFish","alice"))

    async def test_history_does_not_reveal_other_scope_or_user_and_survives_restart(self):
        first=message("private-a")
        await self.host.ingest(first)
        other=message("private-b",scope_id="another-private")
        await self.host.ingest(other)
        show=await self.host.relationship_command(other,"history")
        self.assertIn(other.event_id,show)
        self.assertNotIn(first.event_id,show)
        self.assertIn("尚无",await self.host.relationship_command(message(user_id="bob")))
        await self.host.stop()
        self.host=PersonaHost(HostSettings(),Path(self.directory.name),clock=lambda:self.now)
        await self.host.start()
        await self.host.scheduler.stop()
        self.assertEqual((await self.context()).relationship_actor_id,ACTOR)

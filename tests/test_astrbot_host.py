"""插件宿主、QQ 边界和包命名空间；由开发者手动运行。"""

import importlib.util
import json
import logging
import sqlite3
from contextlib import closing
import subprocess
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, AsyncMock

from pydantic import ValidationError

from persona_agent.application.host import PersonaHost
from persona_agent.activity.feedback import ActivityFeedback
from persona_agent.application.qq import adapt_qq_message, message_time_adjustment_id
from persona_agent.core.persistence import CorePersistence
from persona_agent.core.events import AgentEvent
from persona_agent.application.settings import HostSettings
from persona_agent.persistence.errors import EventConflictError


TIME = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class QQEvent:
    def __init__(self, *, group="group-1", platform="aiocqhttp", admin=True, text="你好", message_id="123"):
        self.group, self.platform, self.admin, self.text = group, platform, admin, text
        self.platform_id = "qq-instance-1"
        self.sender_id, self.bot_id = "sender-1", "bot-1"
        self.unified_msg_origin = f"{self.platform_id}:{'GroupMessage' if group else 'FriendMessage'}:{group or self.sender_id}"
        self.message_obj = types.SimpleNamespace(message_id=message_id, timestamp=int(TIME.timestamp()))
        self.extras = {}

    def get_platform_name(self): return self.platform
    def get_platform_id(self): return self.platform_id
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender_id
    def get_self_id(self): return self.bot_id
    def get_message_str(self): return self.text
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def is_admin(self): return self.admin
    def plain_result(self, value): return value


class QQBoundaryTests(unittest.TestCase):
    def test_stable_identity_and_scope_separation(self):
        event = QQEvent()
        first = adapt_qq_message(event)
        self.assertEqual(adapt_qq_message(event), first)
        self.assertEqual(first.scope_id, event.unified_msg_origin)
        self.assertEqual(first.payload["text"], "你好")
        self.assertNotEqual(adapt_qq_message(QQEvent(group="group-2")).event_id, first.event_id)
        event.platform_id = "other-instance"
        self.assertNotEqual(adapt_qq_message(event).event_id, first.event_id)
        with self.assertRaises(ValueError): adapt_qq_message(QQEvent(message_id=""))
        with self.assertRaises(ValueError): adapt_qq_message(QQEvent(platform="telegram"))

    def test_schema_defaults_and_invalid_configuration(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = {key: value["default"] for key, value in schema.items()}
        self.assertEqual(HostSettings.model_validate(defaults), HostSettings())
        with self.assertRaises(ValidationError): HostSettings(autonomous_enabled="true")
        with self.assertRaises(ValidationError): HostSettings(qq_group_ids=[123])

    def test_nested_plugin_namespace_without_global_aliases(self):
        code = '''
import importlib.util, importlib, sys
from pathlib import Path
root = Path.cwd()
before = set(sys.modules)
spec = importlib.util.spec_from_file_location('isolated_plugin', root / '__init__.py', submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
host = importlib.import_module('isolated_plugin.persona_agent.application.host')
assert host.PersonaHost.__module__.startswith('isolated_plugin.persona_agent.')
assert not {'core','activity','application','persona','persistence','infrastructure'} & (set(sys.modules) - before)
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.now = TIME
        self.saved = []
        self.settings = HostSettings(simulation={"needs_interval_seconds": 5, "activity_interval_seconds": 2})
        self.host = self.make_host()

    def make_host(self, settings=None, save=None):
        return PersonaHost(settings or self.settings, Path(self.directory.name), clock=lambda: self.now,
                           persist_controls=save or self.saved.append)

    async def asyncTearDown(self):
        await self.host.stop()
        self.directory.cleanup()

    async def test_start_stop_recovery_and_qq_duplicate_delivery(self):
        await self.host.start()
        original = (await self.host.store.list_active_activities(self.settings.agent_id))[0]
        before = await self.host.store.get_state(self.settings.agent_id)
        event = QQEvent()
        canonical = adapt_qq_message(event)
        await self.host.ingest(canonical)
        event.message_obj.timestamp += 1
        await self.host.ingest(adapt_qq_message(event))
        self.assertEqual(await self.host.store.get_event(canonical.event_id), canonical)
        self.assertIsNone(await self.host.store.get_event(message_time_adjustment_id(self.settings.agent_id, canonical.event_id)))
        # 群消息没有指向角色：不构成一次互动，状态保持不变。
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), before)
        event.text = "内容被更改"
        with self.assertRaises(EventConflictError):
            await self.host.ingest(adapt_qq_message(event))
        await self.host.stop()
        self.now += timedelta(seconds=6)
        self.host = self.make_host()
        await self.host.start()
        active = await self.host.store.list_active_activities(self.settings.agent_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].instance.activity_id, original.instance.activity_id)
        self.assertGreater(active[0].instance.progress, original.instance.progress)
        restored = await self.host.store.get_state(self.settings.agent_id)
        self.assertEqual(restored.needs_updated_at, self.now)
        self.assertLess(restored.needs.energy, before.needs.energy)
        self.assertIn("角色：", await self.host.status())
        self.assertIn("行为：", await self.host.decision())

    async def test_bad_pending_memory_does_not_block_host_and_is_visible_to_admin(self):
        await self.host.start()
        await self.host.scheduler.stop()
        incoming=QQEvent(group="",text="/persona_memory add 我逛了街",message_id="bad-index")
        event=adapt_qq_message(incoming)
        await self.host.memory_command(event,"add","我逛了街")
        before=await self.host.store.get_state(self.settings.agent_id)
        listing=await self.host.memory_command(adapt_qq_message(QQEvent(group="")),"list")
        identifier=listing.split("（",1)[0]
        await self.host.stop()
        with closing(sqlite3.connect(Path(self.directory.name)/"persona.sqlite3")) as db:
            db.execute("UPDATE memories SET record_json=?,index_version=0 WHERE memory_id=?",("{broken-private-json",identifier))
            db.commit()
        self.host=self.make_host()
        with self.assertLogs("persona_agent.memory.persistence",level="WARNING"):
            await self.host.start()
        await self.host.scheduler.stop()
        self.assertTrue(self.host.ready)
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id),before)
        self.assertIn("本次加载跳过 1 条",await self.host.status())
        pending=await self.host.memory_command(adapt_qq_message(QQEvent(group="")),"pending")
        self.assertIn(identifier,pending)
        self.assertNotIn("broken-private-json",pending)
        context=await self.host.chat_context(event.scope_id,query="逛街")
        self.assertEqual(context.memory_ids,())
        deletion=adapt_qq_message(QQEvent(group="",text="/persona_memory delete "+identifier,message_id="delete-bad-index"))
        self.assertIn("已删除",await self.host.memory_command(deletion,"delete",identifier))
        self.assertIn("没有待修复",await self.host.memory_command(adapt_qq_message(QQEvent(group="")),"pending"))

    async def test_future_platform_timestamp_is_audited_and_replay_survives_clock_catchup(self):
        await self.host.start()
        await self.host.scheduler.stop()  # 本例只验证消息重投，避免时间推进混入生活时钟。
        before = await self.host.store.get_state(self.settings.agent_id)
        event = QQEvent(group="")
        event.message_obj.timestamp = int(self.now.timestamp())+5
        incoming = adapt_qq_message(event)
        await self.host.ingest(incoming)
        stored = await self.host.store.get_event(incoming.event_id)
        self.assertEqual(stored.timestamp, self.now)
        self.assertEqual(stored.payload, incoming.payload)
        audit_id = message_time_adjustment_id(self.settings.agent_id, incoming.event_id)
        audit = await self.host.store.get_event(audit_id)
        self.assertEqual(audit.causation_id, incoming.event_id)
        self.assertEqual(audit.payload["platform_timestamp"], incoming.timestamp.isoformat())
        self.assertEqual(audit.payload["received_at"], self.now.isoformat())
        self.assertEqual(audit.payload["offset_seconds"], 5)
        state = await self.host.store.get_state(self.settings.agent_id)
        self.assertAlmostEqual(state.needs.social_need, before.needs.social_need-.3)
        await self.host.ingest(incoming)
        self.now += timedelta(seconds=6)
        await self.host.ingest(incoming)
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), state)
        self.assertEqual(await self.host.store.get_event(audit_id), audit)
        self.assertEqual(await self.host.store.get_event(incoming.event_id), stored)
        await self.host.stop()
        self.host = self.make_host()
        await self.host.start()
        await self.host.scheduler.stop()
        restored = await self.host.store.get_state(self.settings.agent_id)
        await self.host.ingest(incoming)
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), restored)
        self.assertEqual(await self.host.store.get_event(audit_id), audit)
        event.text = "内容变化"
        with self.assertRaises(EventConflictError):
            await self.host.ingest(adapt_qq_message(event))

    async def test_legacy_clamped_message_without_audit_still_replays(self):
        await self.host.start()
        incoming = QQEvent(group="")
        incoming.message_obj.timestamp += 5
        raw = adapt_qq_message(incoming)
        legacy = AgentEvent(**{**raw.model_dump(), "timestamp": self.now})
        await self.host.store.record_rule_message(self.settings.agent_id, legacy, now=self.now)
        before = await self.host.store.get_state(self.settings.agent_id)
        await self.host.ingest(raw)
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), before)
        self.assertEqual(await self.host.store.get_event(raw.event_id), legacy)
        audit_id = message_time_adjustment_id(self.settings.agent_id, raw.event_id)
        self.assertIsNone(await self.host.store.get_event(audit_id))

    async def test_time_audit_failure_rolls_back_message_and_state(self):
        await self.host.start()
        before = await self.host.store.get_state(self.settings.agent_id)
        event = QQEvent(group="")
        event.message_obj.timestamp += 5
        incoming = adapt_qq_message(event)
        original = CorePersistence.record_event
        async def fail_audit(core, item):
            if item.event_type == "INTERNAL.message_time_adjusted":
                raise RuntimeError("audit write failed")
            await original(core, item)
        with patch.object(CorePersistence, "record_event", new=fail_audit):
            with self.assertRaises(RuntimeError):
                await self.host.ingest(incoming)
        self.assertIsNone(await self.host.store.get_event(incoming.event_id))
        audit_id = message_time_adjustment_id(self.settings.agent_id, incoming.event_id)
        self.assertIsNone(await self.host.store.get_event(audit_id))
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), before)

    async def test_existing_sixty_second_emotion_clock_can_change_to_three_hundred(self):
        legacy = self.settings.model_dump()
        legacy["simulation"]["initial_emotion"]["dynamics"]["tick_interval_seconds"] = 60
        self.host = self.make_host(HostSettings(**legacy))
        await self.host.start()
        before = await self.host.store.get_state(self.settings.agent_id)
        active = await self.host.store.list_active_activities(self.settings.agent_id)
        await self.host.stop()
        self.now += timedelta(seconds=1)
        self.host = self.make_host()
        await self.host.start()
        after = await self.host.store.get_state(self.settings.agent_id)
        self.assertEqual(after.emotion.dynamics.tick_interval_seconds, 300)
        self.assertEqual(after.emotion.current, before.emotion.current)
        self.assertEqual(after.emotion.baseline, before.emotion.baseline)
        self.assertEqual(after.version, before.version+1)
        self.assertEqual(await self.host.store.list_active_activities(self.settings.agent_id), active)
        await self.host.stop()
        self.host = self.make_host()
        await self.host.start()
        self.assertEqual(await self.host.store.get_state(self.settings.agent_id), after)

    async def test_deferred_appraisal_deadline_runs_after_host_restart(self):
        changed = self.settings.model_dump()
        changed["evaluation"]["semantic"] = {"enabled": True, "timeout_seconds": 3}
        self.host = self.make_host(HostSettings(**changed))
        await self.host.start()
        active = (await self.host.store.list_active_activities(self.settings.agent_id))[0]
        source = await self.host.report_feedback(active.instance.activity_id, ActivityFeedback(
            checkpoint_id="host-appraisal", task="failure", summary="等待体验结果", evaluation_mode="deferred"))
        self.assertEqual(len(await self.host.pending_appraisals()), 1)
        self.assertIn("尚未结算", await self.host.evaluation_status(source.event_id))
        before = await self.host.store.get_state(self.settings.agent_id)
        await self.host.stop()
        self.now += timedelta(seconds=4)
        self.host = self.make_host(HostSettings(**changed))
        await self.host.start()
        after = await self.host.store.get_state(self.settings.agent_id)
        self.assertEqual(after.version, before.version+1)
        self.assertLess(after.emotion.current.valence, 0)
        self.assertEqual(await self.host.pending_appraisals(), ())
        self.assertIn("semantic_timeout", await self.host.evaluation_status(source.event_id))
        self.assertEqual(after.needs, before.needs)

    async def test_controls_persist_and_failed_save_preserves_policy(self):
        await self.host.start()
        await self.host.set_controls(enabled=False)
        await self.host.set_controls(activity_id="rest", disabled=True)
        self.assertFalse(self.saved[-1].autonomous_enabled)
        self.assertIn("rest", self.saved[-1].disabled_definition_ids)
        await self.host.control_activity("pause")
        await self.host.control_activity("resume")
        old = self.host.policy
        def fail(policy): raise OSError("disk write failed")
        self.host._persist_controls = fail
        with self.assertRaises(OSError): await self.host.set_controls(enabled=True)
        self.assertEqual(self.host.policy, old)
        await self.host.stop()
        settings = HostSettings(**{**self.settings.model_dump(), "autonomous_enabled": False,
                                   "disabled_activities": ["rest"]})
        self.host = self.make_host(settings)
        await self.host.start()
        self.assertEqual(self.host.policy, old)

    async def test_startup_failure_closes_resources_and_can_retry(self):
        bad = HostSettings(simulation={"needs_interval_seconds": 5})
        await self.host.start()
        await self.host.stop()
        # 已有速率快照不允许被配置悄悄覆盖。
        changed = bad.model_dump()
        changed["simulation"]["need_rates"]["energy"] = -99
        self.host = self.make_host(HostSettings(**changed))
        with self.assertRaises(ValueError): await self.host.start()
        self.assertFalse(self.host.ready)
        await self.host.stop()
        self.host = self.make_host()
        await self.host.start()
        self.assertTrue(self.host.ready)


class PluginBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """只替身 AstrBot 导入与装饰器；不宣称替代真实 QQ 联调。"""

    async def test_admin_gate_capture_binding_and_control_save_rollback(self):
        def decorator(*args, **kwargs): return lambda function: function
        class Star:
            def __init__(self, context): self.logger = logging.getLogger("test.persona")
        api = types.ModuleType("astrbot.api")
        api.AstrBotConfig = dict
        events = types.ModuleType("astrbot.api.event")
        events.AstrMessageEvent = QQEvent
        class CustomFilter:
            def __init__(self, *args, **kwargs): pass
        events.filter = types.SimpleNamespace(command=decorator, permission_type=decorator, on_llm_request=decorator, after_message_sent=decorator,
                                              custom_filter=decorator, CustomFilter=CustomFilter,
                                              event_message_type=decorator, EventMessageType=types.SimpleNamespace(ALL=1),
                                              PermissionType=types.SimpleNamespace(ADMIN=1))
        providers = types.ModuleType("astrbot.api.provider")
        providers.ProviderRequest = types.SimpleNamespace
        stars = types.ModuleType("astrbot.api.star")
        stars.Star, stars.Context, stars.StarTools = Star, object, object
        package = types.ModuleType("plugin_contract_test")
        package.__path__ = [str(ROOT)]
        spec = importlib.util.spec_from_file_location("plugin_contract_test.main", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        replacements = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                        "astrbot.api.event": events, "astrbot.api.star": stars, "astrbot.api.provider": providers,
                        "plugin_contract_test": package, spec.name: module}
        with patch.dict(sys.modules, replacements):
            spec.loader.exec_module(module)
            class Config(dict):
                def save_config(self): raise OSError("write failed")
            config = Config(autonomous_enabled=True, disabled_activities=[])
            plugin = module.PersonaPlugin(object(), config)
            self.assertIsNone(await plugin._command(QQEvent(admin=False), "scope"))
            self.assertIsNone(await plugin._command(QQEvent(platform="telegram"), "scope"))
            self.assertIn("group-1", await plugin._command(QQEvent(), "scope"))
            plugin.host = types.SimpleNamespace(ready=True, settings=HostSettings(qq_group_ids=["group-1"]))
            self.assertTrue(plugin._capture_allowed(QQEvent()))
            self.assertTrue(module.QQCaptureFilter().filter(QQEvent(), {}))
            self.assertFalse(plugin._capture_allowed(QQEvent(group="other")))
            self.assertFalse(module.QQCaptureFilter().filter(QQEvent(group="other"), {}))
            self.assertFalse(plugin._capture_allowed(QQEvent(group="")))
            from plugin_contract_test.persona_agent.core.arbiter import ArbitrationPolicy
            with self.assertRaises(OSError): plugin._save_controls(ArbitrationPolicy(autonomous_enabled=False))
            self.assertTrue(config["autonomous_enabled"])
            self.assertEqual(config["disabled_activities"], [])
            with TemporaryDirectory() as directory:
                settings = HostSettings(qq_private_ids=["sender-1"], qq_group_ids=["group-1"])
                plugin.host = module.PersonaHost(settings, Path(directory), clock=lambda: TIME)
                await plugin.host.start()
                try:
                    event = QQEvent(group="")
                    other_part = {"type": "text", "text": "其他插件的内容"}
                    original_contexts = [{"role": "user", "content": "原历史"}]
                    req = types.SimpleNamespace(system_prompt="AstrBot 原人设", extra_user_content_parts=[other_part],
                                                prompt="原提问", contexts=original_contexts, func_tool=object())
                    before = await plugin.host.store.get_state(settings.agent_id)
                    await plugin.attach_chat_context(event, req)
                    self.assertTrue(req.system_prompt.startswith("AstrBot 原人设"))
                    self.assertEqual(len(req.extra_user_content_parts), 2)
                    self.assertIs(req.extra_user_content_parts[0], other_part)
                    self.assertIs(req.contexts, original_contexts)
                    self.assertEqual(req.prompt, "原提问")
                    self.assertIn("persona_state=", req.extra_user_content_parts[-1]["text"])
                    await plugin.attach_chat_context(event, req)
                    self.assertEqual(len(req.extra_user_content_parts), 2)
                    self.assertEqual(await plugin.host.store.get_state(settings.agent_id), before)
                    self.assertIn("附加长度", await plugin._command(event, "context", "last"))
                    self.assertIn("不调用模型", await plugin._command(event, "context", "now"))
                    self.assertIsNone(await plugin._command(QQEvent(group="", admin=False), "context"))
                    self.assertIsNone(await plugin._command(QQEvent(group="", admin=False), "relationship"))
                    self.assertIn("尚无",await plugin._command(event,"relationship","show"))
                    self.assertIn("尚无",await anext(plugin.persona_relationship(event)))
                    denied = QQEvent(group="")
                    denied.sender_id = "unbound"
                    await plugin.attach_chat_context(denied, req)
                    self.assertEqual(req.system_prompt, "AstrBot 原人设")
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                    self.assertIn("未绑定",await plugin._command(denied,"relationship"))
                    await plugin.attach_chat_context(QQEvent(), req)
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                    group_settings = settings.model_dump()
                    group_settings["chat_context"]["private_only"] = False
                    plugin.host.settings = module.HostSettings(**group_settings)
                    group = QQEvent()
                    group.is_at_or_wake_command = False
                    await plugin.attach_chat_context(group, req)
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                    group.is_at_or_wake_command = True
                    await plugin.attach_chat_context(group, req)
                    self.assertEqual(len(req.extra_user_content_parts), 2)
                    plugin.host.settings = module.HostSettings(**settings.model_dump())
                    event.set_extra("handlers_parsed_params", {"command": "other"})
                    await plugin.attach_chat_context(event, req)
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                    event.set_extra("handlers_parsed_params", {})
                    with patch.object(plugin.host, "chat_context", AsyncMock(side_effect=RuntimeError("read failed"))):
                        await plugin.attach_chat_context(event, req)
                    self.assertEqual(req.system_prompt, "AstrBot 原人设")
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                    self.assertIsNone(await plugin._command(QQEvent(group="",admin=False),"memory","list"))
                    self.assertIn("未绑定",await plugin._command(denied,"memory","list"))
                    self.assertIn("仅在绑定私聊",await plugin._command(QQEvent(),"memory","list"))
                    add=QQEvent(group="",text="/persona_memory add 我明天会出门",message_id="memory-add")
                    self.assertIn("已新增",(await anext(plugin.persona_memory(add))))
                    more=QQEvent(group="",text="/persona_memory add 我明天去逛街 看电影",message_id="memory-enrich")
                    self.assertIn("已补充",(await anext(plugin.persona_memory(more))))
                    event.text="我的未来计划是什么"
                    await plugin.attach_chat_context(event,req)
                    actual=plugin.host.last_context(event.unified_msg_origin)
                    self.assertEqual(len(actual.memory_ids),1)
                    self.assertIn("逛街 看电影",actual.user_text)
                    self.assertEqual(await plugin.host.store.get_state(settings.agent_id),before)
                    remove=QQEvent(group="",text="/persona_memory delete "+actual.memory_ids[0],message_id="memory-delete")
                    self.assertIn("已删除",(await anext(plugin.persona_memory(remove))))
                    self.assertIsNone(plugin.host.last_context(event.unified_msg_origin))
                    await plugin.attach_chat_context(event,req)
                    self.assertEqual(plugin.host.last_context(event.unified_msg_origin).memory_ids,())
                    changed = settings.model_dump()
                    changed["chat_context"]["enabled"] = False
                    plugin.host.settings = module.HostSettings(**changed)
                    await plugin.attach_chat_context(event, req)
                    self.assertEqual(req.extra_user_content_parts, [other_part])
                finally:
                    await plugin.host.stop()

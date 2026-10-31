"""插件宿主、QQ 边界和包命名空间；由开发者手动运行。"""

import importlib.util
import json
import logging
import subprocess
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from persona_agent.application.host import PersonaHost
from persona_agent.application.qq import adapt_qq_message
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

    def get_platform_name(self): return self.platform
    def get_platform_id(self): return self.platform_id
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender_id
    def get_self_id(self): return self.bot_id
    def get_message_str(self): return self.text
    def get_extra(self, key, default=None): return default
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
        events.filter = types.SimpleNamespace(command=decorator, permission_type=decorator,
                                              custom_filter=decorator, CustomFilter=CustomFilter,
                                              event_message_type=decorator, EventMessageType=types.SimpleNamespace(ALL=1),
                                              PermissionType=types.SimpleNamespace(ADMIN=1))
        stars = types.ModuleType("astrbot.api.star")
        stars.Star, stars.Context, stars.StarTools = Star, object, object
        package = types.ModuleType("plugin_contract_test")
        package.__path__ = [str(ROOT)]
        spec = importlib.util.spec_from_file_location("plugin_contract_test.main", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        replacements = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                        "astrbot.api.event": events, "astrbot.api.star": stars,
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

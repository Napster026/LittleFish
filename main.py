"""AstrBot v4.29.3 插件入口；QQ 入站和管理员调试命令。"""

import weakref
import re

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.provider import ProviderRequest

from .persona_agent.application.host import PersonaHost
from .persona_agent.application.qq import adapt_qq_message, is_qq_event, qq_scope
from .persona_agent.application.settings import HostSettings
from .persona_agent.application.chat_context import apply_prompt_context


class QQCaptureFilter(filter.CustomFilter):
    """在唤醒阶段就排除未绑定消息，避免全量监听扩大会话触发范围。"""

    plugin_ref = None

    def filter(self, event, cfg):
        plugin = self.plugin_ref() if self.plugin_ref is not None else None
        return plugin is not None and plugin._capture_allowed(event)


class PersonaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.host = None
        QQCaptureFilter.plugin_ref = weakref.ref(self)

    def _save_controls(self, policy):
        old_enabled = self.config["autonomous_enabled"]
        old_disabled = self.config["disabled_activities"]
        self.config["autonomous_enabled"] = policy.autonomous_enabled
        self.config["disabled_activities"] = sorted(policy.disabled_definition_ids)
        try:
            self.config.save_config()
        except BaseException:
            self.config["autonomous_enabled"], self.config["disabled_activities"] = old_enabled, old_disabled
            raise

    async def initialize(self):
        settings = HostSettings.model_validate(dict(self.config))
        self.host = PersonaHost(settings, StarTools.get_data_dir("astrbot_plugin_persona"),
                                persist_controls=self._save_controls)
        try:
            await self.host.start()
        except BaseException:
            self.host = None
            raise
        report=self.host.store.memory_index_report
        if report.skipped:
            samples=", ".join(repr(identifier) for identifier in report.sample_ids)
            self.logger.warning(f"Persona 记忆索引降级：跳过 {report.skipped} 条异常记录，插件继续运行；ID 样例：{samples}。可用 /persona_memory pending 查看当前会话。")
        self.logger.info("Persona Agent 已就绪，使用 /persona_scope 和 /persona_status 开始 QQ 联调")

    async def terminate(self):
        host, self.host = self.host, None
        if host is not None:
            await host.stop()

    def _capture_allowed(self, event):
        if self.host is None or not self.host.ready or not is_qq_event(event):
            return False
        scope = qq_scope(event)
        cfg = self.host.settings
        if cfg.qq_platform_ids and scope["platform_id"] not in cfg.qq_platform_ids:
            return False
        if scope["sender_id"] == scope["bot_id"]:
            return False
        return (scope["group_id"] in cfg.qq_group_ids if scope["group_id"]
                else scope["sender_id"] in cfg.qq_private_ids)

    @filter.custom_filter(QQCaptureFilter, False)
    async def receive_message(self, event: AstrMessageEvent):
        if not self._capture_allowed(event) or event.get_extra("handlers_parsed_params", {}):
            return
        try:
            await self.host.ingest(adapt_qq_message(event),
                                   directed_to_agent=bool(getattr(event, "is_at_or_wake_command", False)))
        except Exception:
            self.logger.exception("Persona QQ 入站处理失败")

    def _chat_context_allowed(self, event):
        if not self._capture_allowed(event) or not self.host.settings.chat_context.enabled:
            return False
        if event.get_group_id():
            return (not self.host.settings.chat_context.private_only
                    and bool(getattr(event, "is_at_or_wake_command", False)))
        return True

    @filter.on_llm_request()
    async def attach_chat_context(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            # 也清除同一请求对象先前属于本插件的块，避免重试或跨会话复用残留。
            apply_prompt_context(req, None)
            if not self._chat_context_allowed(event) or event.get_extra("handlers_parsed_params", {}):
                return
            context = await self.host.chat_context(event.unified_msg_origin,query=event.get_message_str(),
                speaker_id=f"{event.get_platform_id()}:user:{event.get_sender_id()}",is_private=not bool(event.get_group_id()))
            if context is not None:
                apply_prompt_context(req, context)
                self.host.remember_context(context)
        except Exception:
            self.logger.exception("Persona 上下文组装失败，沿用原聊天请求")

    async def _command(self, event, operation, argument="", activity_id=""):
        # 装饰器以外保留入口校验，避免直接调用绕过管理员限制。
        if not is_qq_event(event) or not event.is_admin():
            return None
        if operation == "scope":
            return "\n".join(f"{key}：{value}" for key, value in qq_scope(event).items())
        if self.host is None or not self.host.ready:
            return "Persona Agent 尚未就绪，请检查插件加载日志"
        try:
            if operation == "status":
                return await self.host.status()
            if operation == "decision":
                return await self.host.decision()
            if operation == "evaluation":
                return await self.host.evaluation_status(argument)
            if operation == "feedback":
                return await self.host.feedback_status(argument)
            if operation == "context":
                if not self._chat_context_allowed(event):
                    return "本会话未启用聊天上下文，请检查绑定范围和 private_only 设置"
                if argument not in ("", "now", "last"):
                    return "用法：/persona_context [now|last]"
                if argument == "last":
                    context = self.host.last_context(event.unified_msg_origin)
                    return "暂无本次加载实际注入的上下文" if context is None else context.preview()
                context = await self.host.chat_context(event.unified_msg_origin,
                    speaker_id=f"{event.get_platform_id()}:user:{event.get_sender_id()}",is_private=not bool(event.get_group_id()))
                return "当前上下文预览（不调用模型）：\n"+context.preview()
            if operation == "memory":
                if not self._capture_allowed(event):
                    return "本会话未绑定，不能操作记忆"
                return await self.host.memory_command(adapt_qq_message(event),argument,activity_id)
            if operation in ("pause", "resume"):
                await self.host.set_controls(enabled=operation == "resume")
                return ("自主启动已暂停，现有活动继续运行。需要暂停活动时使用 /persona_activity pause。"
                        if operation == "pause" else "自主启动已恢复，将在后续检查时仲裁。")
            if operation in ("disable", "enable"):
                await self.host.set_controls(activity_id=argument, disabled=operation == "disable")
                return f"活动 {argument} 已{'禁用' if operation == 'disable' else '启用'}，设置已保存。"
            if operation == "activity":
                record = await self.host.control_activity(argument, activity_id)
                return f"{record.definition.name}：{record.instance.status.value}"
            return "\n".join((
                "/persona_scope：查看 QQ 会话标识", "/persona_status：查看角色、活动和目录",
                "/persona_decision：查看最近仲裁", "/persona_pause / /persona_resume：暂停或恢复自主启动",
                "/persona_evaluation [原事件ID]：查看评价、等待状态与有效冲击",
                "/persona_disable 活动定义ID / /persona_enable 活动定义ID",
                "/persona_activity pause|resume|stop [活动实例ID]",
                "/persona_feedback [活动实例ID]：查看阶段内容",
                "/persona_context [now|last]：预览当前或最近实际注入的上下文",
                "/persona_memory add|list|pending|show|history|delete|search：管理当前会话记忆",
            ))
        except (ValueError, RuntimeError) as exc:
            self.logger.exception("Persona 管理操作失败")
            return f"操作未完成：{exc}"
        except Exception:
            self.logger.exception("Persona 管理操作失败")
            return "操作失败，请查看插件日志"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_scope")
    async def persona_scope(self, event: AstrMessageEvent):
        if (text := await self._command(event, "scope")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_status")
    async def persona_status(self, event: AstrMessageEvent):
        if (text := await self._command(event, "status")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_decision")
    async def persona_decision(self, event: AstrMessageEvent):
        if (text := await self._command(event, "decision")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_pause")
    async def persona_pause(self, event: AstrMessageEvent):
        if (text := await self._command(event, "pause")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_resume")
    async def persona_resume(self, event: AstrMessageEvent):
        if (text := await self._command(event, "resume")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_disable")
    async def persona_disable(self, event: AstrMessageEvent, activity_id: str):
        if (text := await self._command(event, "disable", activity_id)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_enable")
    async def persona_enable(self, event: AstrMessageEvent, activity_id: str):
        if (text := await self._command(event, "enable", activity_id)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_activity")
    async def persona_activity(self, event: AstrMessageEvent, action: str, activity_id: str = ""):
        if (text := await self._command(event, "activity", action, activity_id)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_help")
    async def persona_help(self, event: AstrMessageEvent):
        if (text := await self._command(event, "help")) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_evaluation")
    async def persona_evaluation(self, event: AstrMessageEvent, source_event_id: str = ""):
        if (text := await self._command(event, "evaluation", source_event_id)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_feedback")
    async def persona_feedback(self, event: AstrMessageEvent, activity_id: str = ""):
        if (text := await self._command(event, "feedback", activity_id)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_context")
    async def persona_context(self, event: AstrMessageEvent, mode: str = ""):
        if (text := await self._command(event, "context", mode)) is not None:
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("persona_memory")
    async def persona_memory(self, event: AstrMessageEvent):
        # 官方命令匹配后从原文读取剩余内容，保留包含空格的整段正文。
        match=re.search(r"(?:^|[\s/])persona_memory(?:\s+(\S+))?(?:\s+(.*))?$",event.get_message_str().strip(),re.S)
        action="list" if match is None or match.group(1) is None else match.group(1)
        value="" if match is None or match.group(2) is None else match.group(2).strip()
        if (text := await self._command(event,"memory",action,value)) is not None:
            yield event.plain_result(text)

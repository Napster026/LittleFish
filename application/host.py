"""单角色插件组装与生命周期；AstrBot API 保留在 main.py。"""

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..activity.executors.simulated import SimulatedActivityExecutor
from ..activity.manager import ActivityManager
from ..activity.models import ExecutionMode
from ..activity.registry import ExecutorRegistry
from ..activity.service import ActivityService
from ..core.arbiter import ArbitrationPolicy, BehaviorArbiter, BehaviorDecision
from ..core.events import AgentEvent
from ..core.router import EventRouter
from ..core.runtime import AgentRuntime
from ..core.scheduler import TimeScheduler, normalize_schedule_time
from ..core.state import AgentState
from ..infrastructure.storage import SQLiteStorage
from ..persistence.errors import EventConflictError
from .autonomy import AutonomousActivityService
from .appraisal_persistence import DUE_EVENT_TYPE, REQUEST_EVENT_TYPE, AppraisalJob
from .evaluation import evaluation_id
from .emotion import EMOTION_IMPULSE_EVENT_TYPE, EMOTION_TICK_EVENT_TYPE, EmotionEventHandler
from .needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler
from .persistence import PersonaStore
from .settings import HostSettings
from ..cognition.evaluation import RuleEvaluation
from ..activity.feedback import ActivityFeedback
from ..cognition.prompt_builder import build_prompt_context


class PersonaHost:
    def __init__(self, settings: HostSettings, data_dir: Path, *, clock=None,
                 persist_controls: Callable[[ArbitrationPolicy], None] | None = None):
        self.settings = HostSettings.model_validate(settings.model_dump())
        self.data_dir = Path(data_dir)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.store = PersonaStore(SQLiteStorage(self.data_dir / "persona.sqlite3"), evaluation=self.settings.evaluation)
        self.selector = self.settings.simulation.selector()
        self.policy = self.settings.arbitration()
        self._persist_controls = persist_controls
        self._lifecycle = asyncio.Lock()
        self._operations = asyncio.Lock()
        self._shutdown_task = None
        self.ready = False
        self.last_message_id = None
        self._last_contexts = OrderedDict()
        self.runtime = self.activities = self.autonomy = self.scheduler = None

    def _autonomy(self, policy):
        return AutonomousActivityService(self.settings.agent_id, self.store, self.selector,
                                         self.activities, self.runtime, clock=self.clock,
                                         arbiter=BehaviorArbiter(policy))

    async def start(self):
        async with self._lifecycle:
            if self.ready:
                return
            if self._shutdown_task is not None and not self._shutdown_task.done():
                raise RuntimeError("角色正在关闭，请稍后重试")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            await self.store.open()
            try:
                agent_id = self.settings.agent_id
                cfg = self.settings.simulation
                now = normalize_schedule_time(self.clock())
                state = await self.store.get_state(agent_id)
                if state is None:
                    await self.store.create_state(AgentState(agent_id=agent_id, needs=cfg.initial_needs, updated_at=now))
                    state = await self.store.get_state(agent_id)
                registry = ExecutorRegistry()
                ids = {d.definition_id for d in cfg.activities}
                ids.update(r.instance.definition_id for r in await self.store.list_active_activities(agent_id)
                           if r.instance.mode == ExecutionMode.SIMULATED)
                for identifier in sorted(ids):
                    registry.register(identifier, ExecutionMode.SIMULATED, SimulatedActivityExecutor())
                self.activities = ActivityService(agent_id, self.store, ActivityManager(registry), clock=self.clock,
                                                   tick_interval_seconds=cfg.activity_interval_seconds)
                router = EventRouter()
                router.register(NEEDS_TICK_EVENT_TYPE, NeedTickHandler(clock=self.clock))
                emotional = EmotionEventHandler(clock=self.clock)
                router.register(EMOTION_TICK_EVENT_TYPE, emotional)
                router.register(EMOTION_IMPULSE_EVENT_TYPE, emotional)
                await self.store.start_needs_clock(agent_id, rates=cfg.need_rates, now=now,
                                                   interval_seconds=cfg.needs_interval_seconds)
                if state.emotion is not None:
                    if cfg.initial_emotion is not None and (
                        state.emotion.baseline != cfg.initial_emotion.baseline
                        or state.emotion.dynamics.model_dump(exclude={"tick_interval_seconds"})
                        != cfg.initial_emotion.dynamics.model_dump(exclude={"tick_interval_seconds"})
                    ):
                        raise ValueError("已保存的情绪基线或参数与配置不同，不能自动重置")
                    await self.store.start_emotion_clock(agent_id, now=now,
                        tick_interval_seconds=None if cfg.initial_emotion is None
                        else cfg.initial_emotion.dynamics.tick_interval_seconds)
                elif cfg.initial_emotion is not None:
                    await self.store.start_emotion_clock(agent_id, now=now, initial=cfg.initial_emotion.at(now))
                self.runtime = AgentRuntime(agent_id, self.store, router, clock=self.clock)
                await self.runtime.start()
                self.autonomy = self._autonomy(self.policy)
                self.scheduler = TimeScheduler(agent_id, self.store, self._deliver, clock=self.clock)
                # 先处理一批离线期间到期计划，再做启动仲裁；后台继续处理余下计划。
                await self.scheduler.run_due()
                await self.store.recover_appraisals(agent_id, now=self.clock())
                await self.autonomy.recover()
                await self.scheduler.start()
                self.ready = True
                self._shutdown_task = None
            except BaseException:
                self.ready = False
                await self._shutdown()
                raise

    async def _shutdown(self):
        try:
            if self.scheduler is not None:
                await self.scheduler.stop()
        finally:
            async with self._operations:
                try:
                    if self.runtime is not None:
                        await self.runtime.stop()
                finally:
                    await self.store.close()
        self.runtime = self.activities = self.autonomy = self.scheduler = None
        self._last_contexts.clear()

    async def stop(self):
        async with self._lifecycle:
            self.ready = False
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(self._shutdown())
            task = self._shutdown_task
        await asyncio.shield(task)

    def _check(self):
        if not self.ready:
            raise RuntimeError("角色尚未就绪或正在关闭")

    async def _deliver(self, event):
        async with self._operations:
            if event.event_type == DUE_EVENT_TYPE:
                receipt = await self.store.handle_appraisal_deadline(self.settings.agent_id, event, now=self.clock())
                await self._consider_appraisal(receipt)
            else:
                await self.autonomy.handle_event(event)

    async def _consider_appraisal(self, receipt):
        result = RuleEvaluation.model_validate(receipt.payload["evaluation"])
        if result.needs_delta is not None or (result.impulse is not None and self.selector.emotion_influence_enabled):
            await self.autonomy.consider(receipt.event_id)

    async def pending_appraisals(self, *, limit=20):
        async with self._operations:
            self._check()
            return await self.store.pending_appraisals(self.settings.agent_id, limit=limit)

    async def complete_appraisal(self, source_event_id, raw):
        async with self._operations:
            self._check()
            receipt = await self.store.complete_appraisal(self.settings.agent_id, source_event_id, raw, now=self.clock())
            await self._consider_appraisal(receipt)
            return receipt

    async def ingest(self, event: AgentEvent, *, directed_to_agent=False):
        async with self._operations:
            self._check()
            if event.event_type != "SOCIAL.message_received":
                raise ValueError("消息入口只接收 SOCIAL.message_received")
            now = normalize_schedule_time(self.clock())
            platform_timestamp = None
            existing = await self.store.get_event(event.event_id)
            if existing is not None:
                # 重投先复用首条规范事件；不重新钳制或改写时间审计记录。
                if existing.model_dump(exclude={"timestamp"}) != event.model_dump(exclude={"timestamp"}):
                    raise EventConflictError("同一 QQ 消息 ID 的内容或作用域发生变化")
                event = existing
            elif event.timestamp > now:
                # 接收时间用于结算；平台原始时间另存关联审计，与原消息原子提交。
                platform_timestamp = event.timestamp
                event = AgentEvent(**{**event.model_dump(), "timestamp": now})
            receipt = await self.store.record_rule_message(self.settings.agent_id, event, now=now,
                directed=directed_to_agent, platform_timestamp=platform_timestamp)
            result = await self.runtime.submit(event)
            self.last_message_id = event.event_id
            await self._consider_appraisal(receipt)
            return result

    async def report_feedback(self, activity_id: str, feedback: ActivityFeedback, *, observed_at=None, provenance=None, scope_id=None):
        async with self._operations:
            self._check()
            event = await self.store.report_activity_feedback(self.settings.agent_id, activity_id, feedback,
                now=self.clock(), observed_at=observed_at, provenance=provenance, scope_id=scope_id)
            receipt = await self.store.get_event(evaluation_id(self.settings.agent_id, event.event_id))
            if receipt is not None:
                await self._consider_appraisal(receipt)
            return event

    async def chat_context(self, scope_id):
        async with self._operations:
            self._check()
            cfg = self.settings.chat_context
            if not cfg.enabled:
                return None
            snapshot = await self.store.chat_snapshot(self.settings.agent_id, scope_id, now=self.clock(),
                rates=self.settings.simulation.need_rates,
                labels={item.definition_id: item.name for item in self.settings.simulation.activities},
                include_feedback=cfg.include_feedback)
            return build_prompt_context(snapshot, cfg)

    def remember_context(self, context):
        self._last_contexts[context.scope_id] = context
        self._last_contexts.move_to_end(context.scope_id)
        while len(self._last_contexts) > 64:
            self._last_contexts.popitem(last=False)

    def last_context(self, scope_id):
        return self._last_contexts.get(scope_id)

    async def set_controls(self, *, enabled=None, activity_id=None, disabled=None):
        async with self._operations:
            self._check()
            ids = set(self.policy.disabled_definition_ids)
            if activity_id is not None:
                known = {d.definition_id for d in self.selector.definitions}
                known.update(r.instance.definition_id for r in await self.store.list_active_activities(self.settings.agent_id))
                if activity_id not in known:
                    raise ValueError("活动 ID 不存在，请先查看活动目录")
                if disabled:
                    ids.add(activity_id)
                else:
                    ids.discard(activity_id)
            policy = ArbitrationPolicy(autonomous_enabled=self.policy.autonomous_enabled if enabled is None else enabled,
                                       disabled_definition_ids=ids)
            replacement = self._autonomy(policy)
            replacement.last_decision = self.autonomy.last_decision
            if self._persist_controls is None:
                raise RuntimeError("未配置控制项持久化，拒绝仅在内存修改")
            self._persist_controls(policy)
            self.policy, self.autonomy = policy, replacement

    async def control_activity(self, action, activity_id=""):
        async with self._operations:
            self._check()
            methods = {"pause": self.activities.pause_activity, "resume": self.activities.resume_activity,
                       "stop": self.activities.stop_activity}
            if action not in methods:
                raise ValueError("活动操作只能为 pause、resume 或 stop")
            if not activity_id:
                active = await self.store.list_active_activities(self.settings.agent_id)
                if len(active) != 1:
                    raise ValueError("当前不是唯一活动，请显式提供活动实例 ID")
                activity_id = active[0].instance.activity_id
            return await methods[action](activity_id)

    async def status(self):
        async with self._operations:
            self._check()
            state = await self.store.get_state(self.settings.agent_id)
            active = await self.store.list_active_activities(self.settings.agent_id)
            needs = state.needs
            lines = [f"角色：{state.agent_id}（状态版本 {state.version}）",
                     f"自主启动：{'开启' if self.policy.autonomous_enabled else '暂停'}",
                     f"需求：精力 {needs.energy:.1f}，饱食 {needs.satiety:.1f}，无聊 {needs.boredom:.1f}，社交 {needs.social_need:.1f}"]
            if state.emotion is not None:
                vad = state.emotion.current
                lines.append(f"已结算情绪 VAD：{vad.valence:.3f} / {vad.arousal:.3f} / {vad.dominance:.3f}")
            for record in active:
                item = record.instance
                progress = "未知" if item.progress is None else f"{item.progress:.0%}"
                lines.append(f"活动：{record.definition.name} [{item.status.value}] {progress}\n实例：{item.activity_id}")
            if not active:
                lines.append("活动：空闲")
            lines.append("目录：" + ", ".join(d.definition_id for d in self.selector.definitions))
            lines.append("禁用：" + (", ".join(sorted(self.policy.disabled_definition_ids)) or "无"))
            lines.append("调度：" + (self.scheduler.last_error or "正常"))
            lines.append("本次加载最近入站：" + (self.last_message_id or "尚未收到绑定范围内的 QQ 消息"))
            lines.append("规则评价：" + ("开启" if self.settings.evaluation.enabled else "关闭"))
            lines.append("附带语义评价：" + ("开启" if self.settings.evaluation.semantic.enabled else "关闭"))
            return "\n".join(lines)

    async def evaluation_status(self, source_event_id=""):
        async with self._operations:
            self._check()
            agent_id = self.settings.agent_id
            if source_event_id:
                receipt = await self.store.get_event(evaluation_id(agent_id, source_event_id))
                if receipt is None:
                    job = await self.store.get_appraisal(agent_id, source_event_id)
                    receipt = None if job is None else job.event()
            else:
                receipt = await self.store.latest_evaluation(agent_id)
            if receipt is None:
                return "暂无评价"
            if receipt.event_type == REQUEST_EVENT_TYPE:
                job = AppraisalJob.model_validate(receipt.payload["job"])
                return "\n".join(("评价方式：等待已有模型调用的体验结果", f"原事件：{job.request.source_event_id}",
                    f"截止时间：{job.deadline.isoformat()}", "情绪尚未结算；超时将采用规则兜底"))
            result = RuleEvaluation.model_validate(receipt.payload["evaluation"])
            return "\n".join(("评价方式："+receipt.payload.get("layer", "rule"), f"原事件：{receipt.causation_id}",
                f"规则：{result.rule_id or '无'}，原因：{result.reason}",
                "有效冲击：" + (str(result.impulse.model_dump()) if result.impulse is not None else "无"),
                "需求变化：" + (str(result.needs_delta.model_dump()) if result.needs_delta is not None else "无"),
                f"需求判定：{result.needs_reason or '无'}"))

    async def feedback_status(self, activity_id=""):
        async with self._operations:
            self._check()
            event = await self.store.latest_activity_feedback(self.settings.agent_id, activity_id or None)
            if event is None:
                return "暂无阶段反馈"
            feedback = ActivityFeedback.model_validate(event.payload["feedback"])
            return "\n".join((f"活动：{event.activity_id}", f"反馈事件：{event.event_id}", f"阶段标识：{feedback.checkpoint_id}",
                f"范围：{feedback.scope}，任务：{feedback.task or feedback.outcome}，来源：{event.provenance.value}", f"内容：{feedback.summary}",
                f"内容引用：{feedback.content_ref or '无'}"))

    async def decision(self):
        async with self._operations:
            self._check()
            decision = self.autonomy.last_decision
            if decision is None:
                receipt = await self.store.get_latest_behavior_event(self.settings.agent_id)
                if receipt is not None and "arbitration" in receipt.payload:
                    decision = BehaviorDecision.model_validate(receipt.payload["arbitration"])
            if decision is None:
                return "暂无可读取的仲裁结果"
            lines = [f"行为：{decision.winner.kind.value}，原因：{decision.reason}"]
            for item in decision.assessments[:10]:
                score = item.candidate.score_breakdown
                detail = "" if score is None else f"，需求 {score.need_score:.4f} + 情绪 {score.emotion_adjustment:.4f} = {score.final_score:.4f}"
                lines.append(f"{item.candidate.candidate_id}：{item.reason}{detail}")
            return "\n".join(lines)

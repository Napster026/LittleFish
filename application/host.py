"""单角色插件组装与生命周期；AstrBot API 保留在 main.py。"""

import asyncio
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
from .emotion import EMOTION_IMPULSE_EVENT_TYPE, EMOTION_TICK_EVENT_TYPE, EmotionEventHandler
from .needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler
from .persistence import PersonaStore
from .settings import HostSettings
from ..cognition.evaluation import RuleEvaluation
from ..activity.feedback import ActivityFeedback


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
                        or state.emotion.dynamics != cfg.initial_emotion.dynamics
                    ):
                        raise ValueError("已保存的情绪基线或参数与配置不同，不能自动重置")
                    await self.store.start_emotion_clock(agent_id, now=now)
                elif cfg.initial_emotion is not None:
                    await self.store.start_emotion_clock(agent_id, now=now, initial=cfg.initial_emotion.at(now))
                self.runtime = AgentRuntime(agent_id, self.store, router, clock=self.clock)
                await self.runtime.start()
                self.autonomy = self._autonomy(self.policy)
                self.scheduler = TimeScheduler(agent_id, self.store, self._deliver, clock=self.clock)
                # 先处理一批离线期间到期计划，再做启动仲裁；后台继续处理余下计划。
                await self.scheduler.run_due()
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
            await self.autonomy.handle_event(event)

    async def ingest(self, event: AgentEvent, *, directed_to_agent=False):
        async with self._operations:
            self._check()
            if event.event_type != "SOCIAL.message_received":
                raise ValueError("消息入口只接收 SOCIAL.message_received")
            existing = await self.store.get_event(event.event_id)
            if existing is not None:
                # 同一 QQ 消息重投可能生成新的接收时间；保留第一次记录的时间。
                if existing.model_dump(exclude={"timestamp"}) != event.model_dump(exclude={"timestamp"}):
                    raise EventConflictError("同一 QQ 消息 ID 的内容或作用域发生变化")
                event = existing
            receipt = await self.store.record_rule_message(self.settings.agent_id, event, now=self.clock(),
                                                            directed=directed_to_agent)
            result = await self.runtime.submit(event)
            self.last_message_id = event.event_id
            evaluation = RuleEvaluation.model_validate(receipt.payload["evaluation"])
            if evaluation.impulse is not None and self.selector.emotion_influence_enabled:
                await self.autonomy.consider(receipt.event_id)
            return result

    async def report_feedback(self, activity_id: str, feedback: ActivityFeedback, *, observed_at=None, provenance=None):
        async with self._operations:
            self._check()
            event = await self.store.report_activity_feedback(self.settings.agent_id, activity_id, feedback,
                now=self.clock(), observed_at=observed_at, provenance=provenance)
            if self.selector.emotion_influence_enabled:
                await self.autonomy.consider(event.event_id)
            return event

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
            lines.append("本地规则评价：" + ("开启" if self.settings.evaluation.enabled else "关闭"))
            return "\n".join(lines)

    async def evaluation_status(self):
        async with self._operations:
            self._check()
            receipt = await self.store.latest_evaluation(self.settings.agent_id)
            if receipt is None:
                return "暂无本地规则评价"
            result = RuleEvaluation.model_validate(receipt.payload["evaluation"])
            return "\n".join(("评价方式：本地规则", f"原事件：{receipt.causation_id}",
                f"规则：{result.rule_id or '无'}，原因：{result.reason}",
                "有效冲击：" + (str(result.impulse.model_dump()) if result.impulse is not None else "无")))

    async def feedback_status(self, activity_id=""):
        async with self._operations:
            self._check()
            event = await self.store.latest_activity_feedback(self.settings.agent_id, activity_id or None)
            if event is None:
                return "暂无阶段反馈"
            feedback = ActivityFeedback.model_validate(event.payload["feedback"])
            return "\n".join((f"活动：{event.activity_id}", f"阶段标识：{feedback.checkpoint_id}",
                f"结果：{feedback.outcome}，来源：{event.provenance.value}", f"内容：{feedback.summary}",
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

"""按活动定义 ID 与执行模式注册执行器。"""

from __future__ import annotations

from activity.executor import ActivityExecutionError, ActivityExecutor, ExecutorCapabilities
from activity.models import ActivityDefinition, ExecutionMode


class ExecutorRegistryError(RuntimeError):
    """执行器注册或查找失败。"""


class DuplicateExecutorError(ExecutorRegistryError):
    """同一活动定义与执行模式已经注册。"""


class ExecutorNotFoundError(ExecutorRegistryError):
    """没有找到该定义与模式对应的执行器。"""


class InvalidExecutorError(ExecutorRegistryError):
    """注册对象缺少统一接口或有效能力声明。"""


class ExecutorRegistry:
    """启动前显式注册，不自动回退模式，也不覆盖已有注册。"""

    def __init__(self):
        self._executors: dict[tuple[str, ExecutionMode], ActivityExecutor] = {}

    def register(
        self, definition_id: str, mode: ExecutionMode, executor: ActivityExecutor
    ) -> None:
        if not isinstance(definition_id, str):
            raise ValueError("definition_id 必须是字符串")
        ActivityDefinition.validate_text(definition_id)
        mode = ExecutionMode(mode)
        if not isinstance(executor, ActivityExecutor):
            raise InvalidExecutorError("执行器必须提供完整的 ActivityExecutor 接口")
        if not isinstance(executor.capabilities, ExecutorCapabilities):
            raise InvalidExecutorError("执行器必须返回 ExecutorCapabilities 能力声明")
        ExecutorCapabilities.model_validate(executor.capabilities.model_dump())
        for name in ("start", "step", "observe", "pause", "resume", "stop", "snapshot"):
            if not callable(getattr(executor, name, None)):
                raise InvalidExecutorError(f"执行器方法 {name} 不可调用")
        key = (definition_id, mode)
        if key in self._executors:
            raise DuplicateExecutorError("该活动定义与执行模式已经注册执行器")
        self._executors[key] = executor

    def resolve(self, definition: ActivityDefinition, mode: ExecutionMode) -> ActivityExecutor:
        definition = ActivityDefinition.model_validate(definition.model_dump())
        mode = ExecutionMode(mode)
        if mode not in definition.supported_modes:
            raise ActivityExecutionError("活动定义不支持所选执行模式")
        executor = self._executors.get((definition.definition_id, mode))
        if executor is None:
            raise ExecutorNotFoundError("没有注册该活动定义与执行模式的执行器")
        return executor

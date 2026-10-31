"""共享持久化契约异常，不依赖领域模型或具体数据库。"""

class StorageError(RuntimeError):
    """存储操作因业务约束或生命周期问题被拒绝。"""


class StorageNotOpenError(StorageError):
    """数据库尚未打开或已经关闭。"""


class UnsupportedSchemaError(StorageError):
    """数据库结构版本不受当前实现支持，不能自动降级。"""


class StateAlreadyExistsError(StorageError):
    """角色状态已存在，不能用初始化操作覆盖。"""


class StateNotFoundError(StorageError):
    """目标角色尚未创建。"""


class EventNotFoundError(StorageError):
    """提案依据事件尚未记录。"""


class EventConflictError(StorageError):
    """相同事件 ID 对应的内容不一致。"""


class ProposalConflictError(StorageError):
    """相同提案 ID 对应的内容不一致。"""


class EventAlreadySettledError(StorageError):
    """同一角色的同一事件已通过另一份提案结算。"""


class ActivityAlreadyExistsError(StorageError):
    """活动实例 ID 已存在，不能重新初始化或覆盖。"""


class ActivityNotFoundError(StorageError):
    """目标角色下没有找到活动实例。"""


class ActivityVersionConflictError(StorageError):
    """候选结果依据的实例快照与当前已保存状态不一致。"""


class ActivityDefinitionConflictError(StorageError):
    """活动开始后不能用同 ID 的另一份定义替换原规则。"""


class ActivityCommitConflictError(StorageError):
    """同一活动旧修订已经提交过不同的操作、时间或结果。"""


class ScheduleConflictError(StorageError):
    """同一调度 ID 不能更换计划内容。"""


class ScheduleNotFoundError(StorageError):
    """角色下没有对应定时计划。"""


class ScheduleVersionConflictError(StorageError):
    """投递记录状态或尝试次数已变化。"""

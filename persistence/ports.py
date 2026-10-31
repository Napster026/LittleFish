"""跨层持久化端口：只传递原始行和条件，不导入任何业务模型。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol


SQLValue = str | int | float | bytes | None
Row = dict[str, SQLValue]


class PersistenceSession(Protocol):
    """事务内的有限行操作；where/at_most/after 内的条件均按 AND 连接。"""

    async def fetch_one(self, table: str, *, where: Row) -> Row | None: ...

    async def fetch_all(
        self, table: str, *, where: Row | None = None, at_most: Row | None = None,
        after: Row | None = None, order_by: tuple[str, ...] = (), limit: int = 100,
    ) -> tuple[Row, ...]: ...

    async def insert(self, table: str, values: Row) -> None: ...

    async def update(self, table: str, values: Row, *, where: Row) -> int: ...


class UnitOfWork(Protocol):
    """会话必须隔离并发写入，正常退出提交，异常或取消回滚且使会话失效。"""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    def transaction(self) -> AbstractAsyncContextManager[PersistenceSession]: ...

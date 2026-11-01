"""跨层持久化端口：只传递原始行和条件，不导入任何业务模型。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol


SQLValue = str | int | float | bytes | None
Row = dict[str, SQLValue]


class PersistenceSession(Protocol):
    """事务内的有限行操作；各条件按 AND 连接；显式指定升降序及有限匹配删除。"""

    def savepoint(self) -> AbstractAsyncContextManager[None]:
        """失败只回滚此块；取消/系统错误仍交给外层事务处理。"""
        ...

    async def fetch_one(self, table: str, *, where: Row) -> Row | None: ...

    async def fetch_all(
        self, table: str, *, where: Row | None = None, at_most: Row | None = None,
        after: Row | None = None, order_by: tuple[str, ...] = (), limit: int = 100,
        before: Row | None = None, at_least: Row | None = None, descending: bool = False,
        where_in: dict[str, tuple[SQLValue, ...]] | None = None,
    ) -> tuple[Row, ...]: ...

    async def insert(self, table: str, values: Row) -> None: ...

    async def update(self, table: str, values: Row, *, where: Row) -> int: ...

    async def delete(self, table: str, *, where: Row) -> int: ...


class UnitOfWork(Protocol):
    """会话必须隔离并发写入，正常退出提交，异常或取消回滚且使会话失效。"""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    def transaction(self) -> AbstractAsyncContextManager[PersistenceSession]: ...

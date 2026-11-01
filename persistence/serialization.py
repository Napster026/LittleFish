"""通用序列化辅助，不解释业务字段。"""

import json
from typing import Any

from .errors import StorageError
from .ports import Row


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def text_column(row: Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise StorageError(f"列 {key} 必须是文本")
    return value


def integer_column(row: Row, key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise StorageError(f"列 {key} 必须是整数")
    return value


def optional_text_column(row: Row, key: str) -> str | None:
    return None if row[key] is None else text_column(row, key)

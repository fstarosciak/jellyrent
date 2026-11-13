"""Mapping of PostgreSQL composite types onto Python dataclasses.

SQLAlchemy has no built-in support for composite (row) types. Instead of
relying on driver-specific adapters, the conversion is done in SQL, which
works with any driver and inside any statement:

    read:   to_jsonb(col)                                -> dict -> dataclass
    write:  jsonb_populate_record(NULL::type, :json)     <- dict <- dataclass
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import cast, func, null
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType


class PgComposite[T](UserDefinedType[T]):
    """Column type for a PostgreSQL composite type backed by a dataclass.

    Constructor argument names equal attribute names on purpose: SQLAlchemy
    copies type objects by reading the constructor's arguments back.
    """

    cache_ok = True

    def __init__(self, type_name: str, dataclass_type: type[T]) -> None:
        if not dataclasses.is_dataclass(dataclass_type):
            raise TypeError(f"{dataclass_type!r} must be a dataclass")
        self.type_name = type_name
        self.dataclass_type = dataclass_type

    @property
    def python_type(self) -> type[T]:
        return self.dataclass_type

    def get_col_spec(self, **kw: Any) -> str:
        return self.type_name

    # -- write path ----------------------------------------------------------
    def bind_processor(self, dialect: Dialect) -> Callable[[T | None], str | None]:
        def process(value: T | None) -> str | None:
            if value is None:
                return None
            return json.dumps(dataclasses.asdict(value))  # type: ignore[call-overload]

        return process

    def bind_expression(self, bindvalue: Any) -> ColumnElement[T]:
        return func.jsonb_populate_record(cast(null(), self), cast(bindvalue, JSONB), type_=self)

    # -- read path -----------------------------------------------------------
    def column_expression(self, colexpr: Any) -> ColumnElement[T]:
        return func.to_jsonb(colexpr, type_=self)

    def result_processor(self, dialect: Dialect, coltype: object) -> Callable[[Any], T | None]:
        dataclass_type = self.dataclass_type
        fields = {f.name for f in dataclasses.fields(dataclass_type)}  # type: ignore[arg-type]

        def process(value: Any) -> T | None:
            if value is None:
                return None
            if isinstance(value, str):  # driver returned jsonb as text
                value = json.loads(value)
            return dataclass_type(**{k: v for k, v in value.items() if k in fields})

        return process

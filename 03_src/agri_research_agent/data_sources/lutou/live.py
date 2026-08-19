"""Fail-closed MySQL reader for approved Lutou direct-database acquisitions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterator, Mapping, Sequence

import pymysql
from pymysql.cursors import DictCursor


_IDENTIFIER = re.compile(r"^[^`\x00-\x1f\x7f]+$")
_READ_ONLY_PRIVILEGES = frozenset({"SELECT", "SHOW VIEW", "USAGE"})


class LutouClientError(RuntimeError):
    """Safe base error that never contains connection or credential values."""


class LutouConnectionError(LutouClientError):
    pass


class LutouReadOnlyError(LutouClientError):
    pass


class LutouSchemaError(LutouClientError):
    pass


class LutouPlanRejectedError(LutouClientError):
    pass


@dataclass(slots=True)
class LutouConnectionSettings:
    host: str = field(repr=False)
    port: int
    user: str = field(repr=False)
    password: str = field(repr=False)
    connect_timeout_seconds: int = 15
    read_timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.host or not self.user or not self.password:
            raise ValueError("Lutou connection settings are incomplete")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Lutou port is invalid")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("Lutou timeouts must be positive")

    def clear_password(self) -> None:
        self.password = ""


@dataclass(frozen=True, slots=True)
class LutouConnectionProof:
    engine_version: str
    engine_comment: str
    global_timezone: str
    session_timezone: str
    global_read_only: bool
    transaction_read_only: bool
    grant_count: int
    write_privileges: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.transaction_read_only or self.write_privileges:
            raise ValueError("Lutou connection proof is not read-only")

    def safe_manifest_fields(self) -> dict[str, object]:
        return {
            "engine_version": self.engine_version,
            "engine_comment": self.engine_comment,
            "global_timezone": self.global_timezone,
            "session_timezone": self.session_timezone,
            "global_read_only": self.global_read_only,
            "transaction_read_only": self.transaction_read_only,
            "grant_count": self.grant_count,
            "write_privileges": list(self.write_privileges),
        }


@dataclass(frozen=True, slots=True)
class LutouQuery:
    schema: str
    table: str
    date_column: str
    value_columns: tuple[str, ...]
    version: str = "three-oil-v1-live/1"
    max_plan_rows: int = 100_000
    max_window_days: int = 20_000

    def __post_init__(self) -> None:
        for value in (self.schema, self.table, self.date_column, *self.value_columns):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError("Lutou query identifier is invalid")
        if not self.value_columns or len(self.value_columns) != len(set(self.value_columns)):
            raise ValueError("Lutou query value columns must be non-empty and unique")
        if self.date_column in self.value_columns:
            raise ValueError("Lutou date column cannot be a value column")
        if self.max_plan_rows <= 0 or self.max_window_days <= 0:
            raise ValueError("Lutou query bounds must be positive")

    @property
    def name(self) -> str:
        return f"lutou.{self.schema}.{self.table}.window"

    @property
    def sql(self) -> str:
        columns = ", ".join(_quote(item) for item in (self.date_column, *self.value_columns))
        relation = f"{_quote(self.schema)}.{_quote(self.table)}"
        date_column = _quote(self.date_column)
        return (
            f"SELECT {columns} FROM {relation} "
            f"WHERE {date_column} >= %s AND {date_column} <= %s "
            f"ORDER BY {date_column}"
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def safe_manifest_fields(self) -> dict[str, object]:
        return {
            "query_name": self.name,
            "query_version": self.version,
            "query_sha256": self.sha256,
            "source_locator": f"database:lutou/schema:{self.schema}/relation:{self.table}",
            "max_plan_rows": self.max_plan_rows,
            "max_window_days": self.max_window_days,
        }


@dataclass(frozen=True, slots=True)
class LutouPlanProof:
    query_sha256: str
    estimated_rows: int
    max_plan_rows: int
    explain_analyze: bool = False

    def __post_init__(self) -> None:
        if self.explain_analyze or not 0 <= self.estimated_rows <= self.max_plan_rows:
            raise ValueError("Lutou query plan exceeds its approved bound")

    def safe_manifest_fields(self) -> dict[str, object]:
        return {
            "query_sha256": self.query_sha256,
            "estimated_rows": self.estimated_rows,
            "max_plan_rows": self.max_plan_rows,
            "explain_analyze": False,
        }


@dataclass(frozen=True, slots=True)
class LutouBatch:
    query: LutouQuery
    plan: LutouPlanProof
    rows: tuple[Mapping[str, object], ...]
    extracted_at: datetime

    def __post_init__(self) -> None:
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() is None:
            raise ValueError("Lutou extracted_at must be timezone-aware")
        if self.plan.query_sha256 != self.query.sha256:
            raise ValueError("Lutou batch query and plan identities differ")


class LutouClient:
    """Read approved, date-bounded tables inside one explicit read-only transaction."""

    def __init__(
        self,
        settings: LutouConnectionSettings,
        *,
        connector: Callable[..., object] = pymysql.connect,
    ) -> None:
        self._settings = settings
        self._connector = connector
        self._connection = None
        self._proof: LutouConnectionProof | None = None

    @property
    def proof(self) -> LutouConnectionProof:
        if self._proof is None:
            raise LutouConnectionError("Lutou client is not connected")
        return self._proof

    def __enter__(self) -> "LutouClient":
        try:
            self._connection = self._connector(
                host=self._settings.host,
                port=self._settings.port,
                user=self._settings.user,
                password=self._settings.password,
                charset="utf8mb4",
                connect_timeout=self._settings.connect_timeout_seconds,
                read_timeout=self._settings.read_timeout_seconds,
                write_timeout=self._settings.connect_timeout_seconds,
                autocommit=False,
                cursorclass=DictCursor,
            )
            self._settings.clear_password()
            self._proof = self._verify_read_only()
            return self
        except LutouClientError:
            self.close()
            raise
        except Exception as exc:
            self._settings.clear_password()
            self.close()
            raise LutouConnectionError(
                f"Lutou connection failed: {type(exc).__name__}"
            ) from None

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._proof = None
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass

    def inspect_query(self, query: LutouQuery) -> tuple[dict[str, object], ...]:
        connection = self._require_connection()
        placeholders = ", ".join(["%s"] * (len(query.value_columns) + 1))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, ORDINAL_POSITION "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    f"AND COLUMN_NAME IN ({placeholders}) ORDER BY ORDINAL_POSITION",
                    (
                        query.schema,
                        query.table,
                        query.date_column,
                        *query.value_columns,
                    ),
                )
                rows = tuple(dict(item) for item in cursor.fetchall())
        except Exception as exc:
            raise LutouSchemaError(
                f"Lutou schema inspection failed: {type(exc).__name__}"
            ) from None
        names = {str(item["COLUMN_NAME"]) for item in rows}
        expected = {query.date_column, *query.value_columns}
        if names != expected:
            raise LutouSchemaError("Lutou approved source columns are missing")
        return rows

    def plan(self, query: LutouQuery, start: date, end: date) -> LutouPlanProof:
        parameters = _window(query, start, end)
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"EXPLAIN FORMAT=JSON {query.sql}", parameters)
                payload = cursor.fetchone()
            raw = next(iter(payload.values()))
            plan = json.loads(str(raw))
            estimates = _plan_row_estimates(plan)
            if not estimates:
                raise ValueError("Lutou plan has no bounded row estimate")
            estimated_rows = max(estimates)
            return LutouPlanProof(query.sha256, estimated_rows, query.max_plan_rows)
        except LutouPlanRejectedError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LutouPlanRejectedError(
                f"Lutou plan was rejected: {type(exc).__name__}"
            ) from None
        except Exception as exc:
            raise LutouClientError(
                f"Lutou query planning failed: {type(exc).__name__}"
            ) from None

    def plan_stream(
        self,
        query: LutouQuery,
        start: date,
        end: date,
        *,
        batch_size: int = 5_000,
    ) -> tuple[LutouPlanProof, Iterator[LutouBatch]]:
        if batch_size <= 0:
            raise ValueError("Lutou batch_size must be positive")
        parameters = _window(query, start, end)
        plan = self.plan(query, start, end)
        return plan, self._stream_after_plan(
            query, parameters, plan, batch_size=batch_size
        )

    def _stream_after_plan(
        self,
        query: LutouQuery,
        parameters: tuple[date, date],
        plan: LutouPlanProof,
        *,
        batch_size: int,
    ) -> Iterator[LutouBatch]:
        connection = self._require_connection()
        extracted_at = datetime.now(timezone.utc)
        try:
            with connection.cursor() as cursor:
                cursor.execute(query.sql, parameters)
                while rows := cursor.fetchmany(batch_size):
                    yield LutouBatch(
                        query=query,
                        plan=plan,
                        rows=tuple(dict(item) for item in rows),
                        extracted_at=extracted_at,
                    )
        except Exception as exc:
            raise LutouClientError(
                f"Lutou read-only query failed: {type(exc).__name__}"
            ) from None

    def _verify_read_only(self) -> LutouConnectionProof:
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW GRANTS FOR CURRENT_USER")
                grant_rows = cursor.fetchall()
                grants = tuple(str(next(iter(item.values()))) for item in grant_rows)
                write_privileges = _write_privileges(grants)
                if write_privileges:
                    raise LutouReadOnlyError(
                        "Lutou account has write-capable privileges"
                    )
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT VERSION() AS engine_version, "
                    "@@version_comment AS engine_comment, "
                    "@@global.time_zone AS global_timezone, "
                    "@@session.time_zone AS session_timezone, "
                    "@@global.read_only AS global_read_only, "
                    "@@session.transaction_read_only AS transaction_read_only"
                )
                row = cursor.fetchone()
                cursor.execute("SELECT 1 AS read_probe")
                if cursor.fetchone()["read_probe"] != 1:
                    raise LutouReadOnlyError("Lutou read probe failed")
            if int(row["transaction_read_only"]) != 1:
                raise LutouReadOnlyError("Lutou transaction is not read-only")
            return LutouConnectionProof(
                engine_version=str(row["engine_version"]),
                engine_comment=str(row["engine_comment"]),
                global_timezone=str(row["global_timezone"]),
                session_timezone=str(row["session_timezone"]),
                global_read_only=bool(row["global_read_only"]),
                transaction_read_only=True,
                grant_count=len(grants),
                write_privileges=write_privileges,
            )
        except LutouClientError:
            raise
        except Exception as exc:
            raise LutouReadOnlyError(
                f"Lutou read-only verification failed: {type(exc).__name__}"
            ) from None

    def _require_connection(self):  # type: ignore[no-untyped-def]
        if self._connection is None:
            raise LutouConnectionError("Lutou client is not connected")
        return self._connection


def _quote(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Lutou identifier is invalid")
    return f"`{value}`"


def _window(query: LutouQuery, start: date, end: date) -> tuple[date, date]:
    if type(start) is not date or type(end) is not date:
        raise ValueError("Lutou query window values must be exact dates")
    if start > end or (end - start).days > query.max_window_days:
        raise ValueError("Lutou query window exceeds its approved bound")
    return start, end


def _write_privileges(grants: Sequence[str]) -> tuple[str, ...]:
    unsafe: set[str] = set()
    for grant in grants:
        normalized = " ".join(grant.upper().split())
        match = re.match(r"^GRANT (.+?) ON ", normalized)
        if match is None:
            unsafe.add("UNRECOGNIZED GRANT")
            continue
        privileges = {item.strip() for item in match.group(1).split(",")}
        unsafe.update(privileges - _READ_ONLY_PRIVILEGES)
        if " WITH GRANT OPTION" in normalized:
            unsafe.add("GRANT OPTION")
    return tuple(sorted(unsafe))


def _plan_row_estimates(value: object) -> tuple[int, ...]:
    results: list[int] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in {"rows_examined_per_scan", "rows_produced_per_join"}:
                    if type(nested) is not int or nested < 0:
                        raise ValueError("Lutou plan row estimate is invalid")
                    results.append(nested)
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(results)

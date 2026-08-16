"""Fail-closed Psycopg client for bounded Tankan read-only extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
from pathlib import Path
from typing import Callable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row

from .models import ConnectionProof, PostgresColumn, QueryPlanProof, QuerySpec, SourceBatch
from .queries import require_approved_query


class TankanClientError(RuntimeError):
    """Safe base error that never includes connection or query parameter values."""


class TankanConnectionError(TankanClientError):
    pass


class TankanReadOnlyError(TankanClientError):
    pass


class TankanSchemaError(TankanClientError):
    pass


class TankanPlanRejectedError(TankanClientError):
    pass


@dataclass(slots=True)
class TankanConnectionSettings:
    host: str = field(repr=False)
    port: int
    database: str
    user: str = field(repr=False)
    password: str = field(repr=False)
    connect_timeout_seconds: int = 15
    statement_timeout_ms: int = 120_000

    @classmethod
    def from_secret_file(cls, path: str | Path) -> "TankanConnectionSettings":
        values: dict[str, str] = {}
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise TankanConnectionError(
                f"Tankan secret file cannot be read: {type(exc).__name__}"
            ) from None
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        required = {
            "TANKAN_HOST",
            "TANKAN_PORT",
            "TANKAN_DATABASE",
            "TANKAN_USER",
            "TANKAN_PASSWORD",
        }
        if required - values.keys():
            raise TankanConnectionError("secret file is missing required Tankan fields")
        try:
            return cls(
                host=values["TANKAN_HOST"],
                port=int(values["TANKAN_PORT"]),
                database=values["TANKAN_DATABASE"],
                user=values["TANKAN_USER"],
                password=values["TANKAN_PASSWORD"],
            )
        except (TypeError, ValueError):
            raise TankanConnectionError("invalid Tankan settings") from None

    def clear_password(self) -> None:
        self.password = ""


class TankanClient:
    def __init__(
        self,
        settings: TankanConnectionSettings,
        *,
        connector: Callable[..., object] = psycopg.connect,
    ) -> None:
        self._settings = settings
        self._connector = connector
        self._connection = None
        self._proof: ConnectionProof | None = None

    @property
    def proof(self) -> ConnectionProof:
        if self._proof is None:
            raise TankanConnectionError("Tankan client is not connected")
        return self._proof

    def __enter__(self) -> "TankanClient":
        try:
            self._connection = self._connector(
                host=self._settings.host,
                port=self._settings.port,
                dbname=self._settings.database,
                user=self._settings.user,
                password=self._settings.password,
                connect_timeout=self._settings.connect_timeout_seconds,
                autocommit=True,
                row_factory=dict_row,
                options=(
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={self._settings.statement_timeout_ms} "
                    "-c lock_timeout=3000 -c idle_in_transaction_session_timeout=600000"
                ),
            )
            self._settings.clear_password()
            self._proof = self._verify_read_only()
            return self
        except TankanClientError:
            self.close()
            raise
        except Exception as exc:
            self._settings.clear_password()
            self.close()
            raise TankanConnectionError(
                f"Tankan connection failed: {type(exc).__name__}"
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

    def _verify_read_only(self) -> ConnectionProof:
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                default_read_only = self._show(cursor, "SHOW default_transaction_read_only")
                autocommit_read_only = self._show(cursor, "SHOW transaction_read_only")
                server_version = self._show(cursor, "SHOW server_version")
                source_timezone = self._show(cursor, "SHOW timezone")
                cursor.execute("SELECT current_database() AS database")
                database = cursor.fetchone()["database"]
            if default_read_only != "on" or autocommit_read_only != "on":
                raise TankanReadOnlyError("Tankan session is not read-only")
            connection.read_only = True
            connection.autocommit = False
            with connection.cursor() as cursor:
                transaction_read_only = self._show(cursor, "SHOW transaction_read_only")
            if transaction_read_only != "on":
                raise TankanReadOnlyError("Tankan transaction is not read-only")
            return ConnectionProof(
                database=str(database),
                server_version=server_version,
                source_timezone=source_timezone,
                default_transaction_read_only=default_read_only,
                transaction_read_only=transaction_read_only,
            )
        except TankanClientError:
            raise
        except Exception as exc:
            raise TankanReadOnlyError(
                f"read-only verification failed: {type(exc).__name__}"
            ) from None

    @staticmethod
    def _show(cursor, statement: str) -> str:  # type: ignore[no-untyped-def]
        cursor.execute(statement)
        return str(next(iter(cursor.fetchone().values())))

    def inspect_relation(self, schema: str, table: str) -> tuple[PostgresColumn, ...]:
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
SELECT ordinal_position, column_name, data_type, udt_name, is_nullable,
       character_maximum_length, datetime_precision
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
""",
                    (schema, table),
                )
                rows = cursor.fetchall()
        except Exception as exc:
            raise TankanSchemaError(
                f"relation inspection failed: {type(exc).__name__}"
            ) from None
        return tuple(
            PostgresColumn(
                ordinal_position=row["ordinal_position"],
                column_name=row["column_name"],
                data_type=row["data_type"],
                udt_name=row["udt_name"],
                nullable=row["is_nullable"] == "YES",
                character_maximum_length=row["character_maximum_length"],
                datetime_precision=row["datetime_precision"],
            )
            for row in rows
        )

    def explain(
        self,
        query: QuerySpec,
        parameters: Sequence[object],
    ) -> QueryPlanProof:
        require_approved_query(query)
        bound_parameters = self._validate_parameters(query, parameters)
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"EXPLAIN (FORMAT JSON) {query.sql}", bound_parameters)
                payload = cursor.fetchone()
            plan = self._extract_plan(payload)
            estimated_rows, total_cost = self._plan_bounds(plan)
            return QueryPlanProof(
                query_sha256=query.sha256,
                estimated_rows=estimated_rows,
                total_cost=total_cost,
                max_plan_rows=query.max_plan_rows,
                max_total_cost=query.max_total_cost,
            )
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise TankanPlanRejectedError(
                f"query plan was rejected: {type(exc).__name__}"
            ) from None
        except Exception as exc:
            raise TankanClientError(
                f"query planning failed: {type(exc).__name__}"
            ) from None

    def stream(
        self,
        query: QuerySpec,
        parameters: Sequence[object],
        *,
        batch_size: int = 10_000,
    ) -> Iterator[SourceBatch]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        bound_parameters = self._validate_parameters(query, parameters)
        plan = self.explain(query, bound_parameters)
        connection = self._require_connection()
        extracted_at = datetime.now(timezone.utc)
        cursor_name = f"tankan_{query.sha256[:12]}"
        try:
            with connection.cursor(name=cursor_name) as cursor:
                cursor.execute(query.sql, bound_parameters)
                while rows := cursor.fetchmany(batch_size):
                    yield SourceBatch(
                        query=query,
                        plan=plan,
                        rows=tuple(dict(row) for row in rows),
                        extracted_at=extracted_at,
                    )
        except TankanClientError:
            raise
        except Exception as exc:
            raise TankanClientError(
                f"read-only query failed: {type(exc).__name__}"
            ) from None

    @staticmethod
    def _validate_parameters(query: QuerySpec, parameters: Sequence[object]) -> tuple[object, ...]:
        values = tuple(parameters)
        if len(values) != query.parameter_count:
            raise ValueError("query parameter count does not match QuerySpec")
        start, end = values
        if type(start) is not date or type(end) is not date:
            raise ValueError("query window parameters must be dates")
        if start > end:
            raise ValueError("query window start must not follow end")
        if (end - start).days > query.max_window_days:
            raise ValueError("query window exceeds QuerySpec maximum")
        return values

    @staticmethod
    def _extract_plan(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or len(payload) != 1:
            raise ValueError("EXPLAIN response is invalid")
        raw = next(iter(payload.values()))
        if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
            raise ValueError("EXPLAIN response is invalid")
        plan = raw[0].get("Plan")
        if not isinstance(plan, dict):
            raise ValueError("EXPLAIN plan is missing")
        return plan

    @classmethod
    def _plan_bounds(cls, plan: dict[str, object]) -> tuple[int, float]:
        rows = plan.get("Plan Rows")
        cost = plan.get("Total Cost")
        if (
            type(rows) is not int
            or not isinstance(cost, (int, float))
            or isinstance(cost, bool)
            or not math.isfinite(float(cost))
        ):
            raise ValueError("EXPLAIN plan bounds are missing")
        maximum_rows = rows
        maximum_cost = float(cost)
        children = plan.get("Plans", [])
        if not isinstance(children, list):
            raise ValueError("EXPLAIN child plans are invalid")
        child_row_total = 0
        for child in children:
            if not isinstance(child, dict):
                raise ValueError("EXPLAIN child plan is invalid")
            child_rows, child_cost = cls._plan_bounds(child)
            child_row_total += child_rows
            maximum_cost = max(maximum_cost, child_cost)
        return max(maximum_rows, child_row_total), maximum_cost

    def _require_connection(self):  # type: ignore[no-untyped-def]
        if self._connection is None:
            raise TankanConnectionError("Tankan client is not connected")
        return self._connection

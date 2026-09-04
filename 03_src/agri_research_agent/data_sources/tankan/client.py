"""Fail-closed Psycopg client for bounded Tankan read-only extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
import re
from pathlib import Path
from typing import Callable, Iterator, Sequence, Mapping
from types import MappingProxyType

import psycopg
from psycopg.rows import dict_row

from .models import ConnectionProof, PostgresColumn, QueryPlanProof, QuerySpec, SourceBatch
from .queries import (require_approved_query, require_approved_live_query,
                      CBOT_SOYBEAN_LIVE_QUERY, DCE_SOYMEAL_LIVE_QUERY,
                      DCE_SOYOIL_LIVE_QUERY, USD_CNH_SPOT_LIVE_QUERY)


class TankanClientError(RuntimeError):
    """Safe base error that never includes connection or query parameter values."""


class TankanConnectionError(TankanClientError):
    pass


class TankanReadOnlyError(TankanClientError):
    pass


class TankanSchemaError(TankanClientError):
    pass


class TankanSourceUnavailableError(TankanClientError):
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


@dataclass(frozen=True)
class LiveReadResult:
    requested: tuple[str, ...]
    available: tuple[Mapping[str, object], ...]
    unavailable: tuple[Mapping[str, object], ...]
    query: QuerySpec
    plan: QueryPlanProof
    connection_proof: ConnectionProof
    retrieved_at: datetime


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

    def latest_source_dates(self) -> dict[str, date]:
        """Read the newest approved source dates without extracting source rows."""
        connection = self._require_connection()
        statements = {
            "market": (
                "SELECT trade_date FROM market.foreign_futures_price_raw "
                "ORDER BY trade_date DESC LIMIT 1"
            ),
            "fx": (
                "SELECT trade_date FROM market.exchange_rate "
                "ORDER BY trade_date DESC LIMIT 1"
            ),
            "domestic_spread": (
                "SELECT trade_date FROM market.futures_spread "
                "ORDER BY trade_date DESC LIMIT 1"
            ),
        }
        output: dict[str, date] = {}
        try:
            with connection.cursor() as cursor:
                for domain, statement in statements.items():
                    cursor.execute(statement)
                    row = cursor.fetchone()
                    value = None if row is None else row.get("trade_date")
                    if isinstance(value, datetime):
                        value = value.date()
                    if type(value) is not date:
                        raise TankanSourceUnavailableError(
                            "approved Tankan source has no latest date"
                        )
                    output[domain] = value
        except TankanClientError:
            raise
        except Exception as exc:
            raise TankanSchemaError(
                f"latest source-date probe failed: {type(exc).__name__}"
            ) from None
        return output

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
        _, batches = self.plan_stream(
            query, parameters, batch_size=batch_size
        )
        yield from batches

    def read_live(self, query: QuerySpec, identities: Sequence[str]) -> LiveReadResult:
        """Allowlisted exact reads; independent of daily date-window APIs."""
        query = require_approved_live_query(query)
        if isinstance(identities, (str, bytes)):
            raise ValueError("live identities must be an explicit sequence")
        requested = tuple(identities)
        if not 1 <= len(requested) <= 32 or any(not isinstance(v, str) for v in requested):
            raise ValueError("live request requires 1..32 exact identities")
        if len(set(requested)) != len(requested):
            raise ValueError("duplicate requested identity")
        prefix, product = "", "大豆"
        if query == USD_CNH_SPOT_LIVE_QUERY:
            if requested != ("USD/CNH:SPOT",):
                raise ValueError("FX requires USD/CNH:SPOT")
            parameters = ("spot",)
        else:
            if query == DCE_SOYMEAL_LIVE_QUERY:
                prefix, product = "M", "豆粕"
            elif query == DCE_SOYOIL_LIVE_QUERY:
                prefix, product = "Y", "豆油"
            pattern = prefix + r"\d{2}(?:0[1-9]|1[0-2])"
            if any(re.fullmatch(pattern, v) is None for v in requested):
                raise ValueError("live request requires full exact YYMM identity")
            # Prefixless and prefixed source representations are the SAME month,
            # never neighboring contracts. Product predicate is independently fixed.
            physical = sorted({v for key in requested for v in (key, key[len(prefix):])})
            parameters = (physical,)
        connection = self._require_connection()
        proof = self.proof
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"EXPLAIN (FORMAT JSON) {query.sql}", parameters)
                plan_rows, plan_cost = self._plan_bounds(self._extract_plan(cursor.fetchone()))
            plan = QueryPlanProof(query.sha256, plan_rows, plan_cost,
                                  query.max_plan_rows, query.max_total_cost)
        except (TypeError, ValueError, KeyError, IndexError):
            raise TankanPlanRejectedError("live query plan rejected") from None
        except Exception as exc:
            raise TankanClientError(f"live planning failed: {type(exc).__name__}") from None
        rows_by_identity: dict[str, list[Mapping[str, object]]] = {key: [] for key in requested}
        retrieved_at = datetime.now(timezone.utc)
        total = 0
        for batch in self._stream_after_plan(query, parameters, plan, batch_size=128):
            retrieved_at = batch.extracted_at
            for row in batch.rows:
                total += 1
                if total > query.max_plan_rows:
                    raise TankanSchemaError("live result exceeds approved bound")
                if query == USD_CNH_SPOT_LIVE_QUERY:
                    if row.get("tenor") != "spot":
                        raise TankanSchemaError("unexpected FX identity")
                    key = "USD/CNH:SPOT"
                else:
                    if row.get("product_name") != product:
                        raise TankanSchemaError("unexpected live product identity")
                    if query == CBOT_SOYBEAN_LIVE_QUERY and row.get("exchange") != "CBOT":
                        raise TankanSchemaError("unexpected live exchange identity")
                    raw = row.get("contract")
                    if not isinstance(raw, str):
                        raise TankanSchemaError("invalid source contract")
                    key = raw if not prefix or raw.startswith(prefix) else prefix + raw
                if key not in rows_by_identity:
                    raise TankanSchemaError("unrequested source exact identity")
                rows_by_identity[key].append(row)
        available, unavailable = [], []
        for key in requested:
            matches = rows_by_identity[key]
            reason = "NOT_FOUND" if not matches else "DUPLICATE_SOURCE_IDENTITY" if len(matches) != 1 else ""
            if not reason:
                row = matches[0]
                price = row.get("mid" if query == USD_CNH_SPOT_LIVE_QUERY else "last")
                stamp = row.get("update_time")
                try:
                    valid_price = not isinstance(price, (bool, str)) and math.isfinite(float(price)) and float(price) > 0
                except (ValueError, TypeError, OverflowError):
                    valid_price = False
                if not valid_price:
                    reason = "INVALID_PRICE"
                elif not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                    reason = "SOURCE_TIMESTAMP_UNAVAILABLE"
                else:
                    available.append(MappingProxyType({**row, "requested_identity": key,
                                                       "price": price, "source_updated_at": stamp}))
            if reason:
                unavailable.append(MappingProxyType({"requested_identity": key, "reason": reason,
                                                     "price": None}))
        return LiveReadResult(requested, tuple(available), tuple(unavailable), query,
                              plan, proof, retrieved_at)

    def plan_stream(
        self,
        query: QuerySpec,
        parameters: Sequence[object],
        *,
        batch_size: int = 10_000,
    ) -> tuple[QueryPlanProof, Iterator[SourceBatch]]:
        """Return the verified plan and a single-use iterator using that plan."""

        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        bound_parameters = self._validate_parameters(query, parameters)
        plan = self.explain(query, bound_parameters)
        return plan, self._stream_after_plan(
            query,
            bound_parameters,
            plan,
            batch_size=batch_size,
        )

    def _stream_after_plan(
        self,
        query: QuerySpec,
        bound_parameters: tuple[object, ...],
        plan: QueryPlanProof,
        *,
        batch_size: int,
    ) -> Iterator[SourceBatch]:
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

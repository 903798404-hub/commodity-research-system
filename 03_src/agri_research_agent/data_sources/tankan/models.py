"""Provider-specific identities and safety proofs for Tankan reads."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from agri_research_agent.research_data import ProviderIdentity


_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL|DO|VACUUM|ANALYZE|REINDEX|CLUSTER|EXPLAIN)\b",
    re.IGNORECASE,
)


def _nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value.strip()


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """A code-reviewed, bounded SELECT and its provider identity."""

    name: str
    version: str
    sql: str
    provider: ProviderIdentity
    parameter_count: int
    max_window_days: int
    max_plan_rows: int
    max_total_cost: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "name"))
        object.__setattr__(self, "version", _nonempty(self.version, "version"))
        object.__setattr__(self, "sql", _nonempty(self.sql, "sql"))
        if not isinstance(self.provider, ProviderIdentity):
            raise TypeError("provider must be ProviderIdentity")
        statement = self.sql.lstrip()
        if not (statement.upper().startswith("SELECT ") or statement.upper().startswith("WITH ")):
            raise ValueError("QuerySpec must contain a read-only SELECT query")
        if (
            ";" in self.sql
            or "--" in self.sql
            or "/*" in self.sql
            or "*/" in self.sql
            or _FORBIDDEN_SQL.search(self.sql)
        ):
            raise ValueError("QuerySpec contains a forbidden SQL token")
        if type(self.parameter_count) is not int or self.parameter_count <= 0:
            raise ValueError("parameter_count must be positive")
        if self.sql.count("%s") != self.parameter_count:
            raise ValueError("parameter_count does not match SQL placeholders")
        if type(self.max_window_days) is not int or self.max_window_days <= 0:
            raise ValueError("max_window_days must be positive")
        if type(self.max_plan_rows) is not int or self.max_plan_rows <= 0:
            raise ValueError("max_plan_rows must be positive")
        if not isinstance(self.max_total_cost, (int, float)) or self.max_total_cost <= 0:
            raise ValueError("max_total_cost must be positive")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def identity(self) -> dict[str, str]:
        return {
            "query_name": self.name,
            "query_version": self.version,
            "query_sha256": self.sha256,
            "dataset_id": str(self.provider.dataset.dataset_id),
            "provider_dataset_id": str(self.provider.provider_dataset_id),
            "origin_system": str(self.provider.dataset.origin_system),
            "acquisition_channel": self.provider.acquisition_channel.value,
            "source_locator": str(self.provider.source_locator),
        }


@dataclass(frozen=True, slots=True)
class LiveQuerySpec:
    """A code-reviewed live-table SELECT bounded by exact contract codes."""

    name: str
    version: str
    sql: str
    provider: ProviderIdentity
    parameter_count: int
    max_contracts: int
    max_plan_rows: int
    max_total_cost: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonempty(self.name, "name"))
        object.__setattr__(self, "version", _nonempty(self.version, "version"))
        object.__setattr__(self, "sql", _nonempty(self.sql, "sql"))
        if not isinstance(self.provider, ProviderIdentity):
            raise TypeError("provider must be ProviderIdentity")
        if not self.sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("LiveQuerySpec must contain a read-only SELECT query")
        if (
            ";" in self.sql
            or "--" in self.sql
            or "/*" in self.sql
            or "*/" in self.sql
            or _FORBIDDEN_SQL.search(self.sql)
        ):
            raise ValueError("LiveQuerySpec contains a forbidden SQL token")
        if type(self.parameter_count) is not int or self.parameter_count not in {0, 1}:
            raise ValueError("live parameter_count must be zero or one")
        if self.sql.count("%s") != self.parameter_count:
            raise ValueError("parameter_count does not match SQL placeholders")
        if type(self.max_contracts) is not int or self.max_contracts < 0:
            raise ValueError("max_contracts must be non-negative")
        if self.parameter_count == 0 and self.max_contracts != 0:
            raise ValueError("zero-parameter live queries cannot accept contracts")
        if self.parameter_count == 1 and self.max_contracts <= 0:
            raise ValueError("contract live queries require a positive bound")
        if type(self.max_plan_rows) is not int or self.max_plan_rows <= 0:
            raise ValueError("max_plan_rows must be positive")
        if not isinstance(self.max_total_cost, (int, float)) or self.max_total_cost <= 0:
            raise ValueError("max_total_cost must be positive")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def identity(self) -> dict[str, str]:
        return {
            "query_name": self.name,
            "query_version": self.version,
            "query_sha256": self.sha256,
            "dataset_id": str(self.provider.dataset.dataset_id),
            "provider_dataset_id": str(self.provider.provider_dataset_id),
            "origin_system": str(self.provider.dataset.origin_system),
            "acquisition_channel": self.provider.acquisition_channel.value,
            "source_locator": str(self.provider.source_locator),
        }


@dataclass(frozen=True, slots=True)
class QueryPlanProof:
    query_sha256: str
    estimated_rows: int
    total_cost: float
    max_plan_rows: int
    max_total_cost: float

    def __post_init__(self) -> None:
        if self.estimated_rows < 0 or self.total_cost < 0 or not math.isfinite(self.total_cost):
            raise ValueError("query plan estimates must be finite and non-negative")
        if self.estimated_rows > self.max_plan_rows or self.total_cost > self.max_total_cost:
            raise ValueError("query plan exceeds its approved bounds")

    def safe_manifest_fields(self) -> dict[str, object]:
        return {
            "query_sha256": self.query_sha256,
            "estimated_rows": self.estimated_rows,
            "total_cost": self.total_cost,
            "max_plan_rows": self.max_plan_rows,
            "max_total_cost": self.max_total_cost,
            "explain_analyze": False,
        }


@dataclass(frozen=True, slots=True)
class PostgresColumn:
    ordinal_position: int
    column_name: str
    data_type: str
    udt_name: str
    nullable: bool
    character_maximum_length: int | None = None
    datetime_precision: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal_position": self.ordinal_position,
            "column_name": self.column_name,
            "data_type": self.data_type,
            "udt_name": self.udt_name,
            "nullable": self.nullable,
            "character_maximum_length": self.character_maximum_length,
            "datetime_precision": self.datetime_precision,
        }


@dataclass(frozen=True, slots=True)
class ConnectionProof:
    database: str
    server_version: str
    source_timezone: str
    default_transaction_read_only: str
    transaction_read_only: str

    def __post_init__(self) -> None:
        if self.default_transaction_read_only != "on" or self.transaction_read_only != "on":
            raise ValueError("connection proof is not read-only")

    def safe_manifest_fields(self) -> dict[str, str]:
        return {
            "database": self.database,
            "postgresql_server_version": self.server_version,
            "source_timezone": self.source_timezone,
            "default_transaction_read_only": self.default_transaction_read_only,
            "transaction_read_only": self.transaction_read_only,
        }


@dataclass(frozen=True, slots=True)
class SourceBatch:
    query: QuerySpec | LiveQuerySpec
    plan: QueryPlanProof
    rows: tuple[Mapping[str, object], ...]
    extracted_at: datetime

    def __post_init__(self) -> None:
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() is None:
            raise ValueError("extracted_at must be timezone-aware")
        if self.plan.query_sha256 != self.query.sha256:
            raise ValueError("query plan identity does not match source query")

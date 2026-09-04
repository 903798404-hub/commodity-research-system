"""Source-preserving Lutou Domestic Basis catalog and direct live adapter."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol

import yaml

from agri_research_agent.data_sources.lutou.live import LutouClient, LutouQuery, LutouSchemaError
from agri_research_agent.market_data.contracts import Exchange
from agri_research_agent.shared.async_update import FreshnessPolicy

DEFAULT_MAPPING_PATH = Path(__file__).resolve().parents[4] / "02_configs" / "lutou_domestic_basis.yaml"
LUTOU_DOMESTIC_BASIS_SCHEMA = "油脂油料价格"
LUTOU_DOMESTIC_BASIS_TABLE = "basis_price"
LUTOU_DOMESTIC_BASIS_COLUMNS = (
    "品种", "文章ID", "文章标题", "行类型", "地区", "省份", "工厂",
    "合同情况", "基差合同年", "基差合同月", "价格原始", "期货合约", "基差",
    "现货价", "成交量", "created_at", "报价类别", "期货收盘价",
)
LUTOU_DOMESTIC_BASIS_TYPES = {
    "日期": "date", "品种": "varchar", "文章ID": "varchar", "文章标题": "varchar",
    "行类型": "varchar", "地区": "varchar", "省份": "varchar", "工厂": "varchar",
    "合同情况": "varchar", "基差合同年": "varchar", "基差合同月": "varchar",
    "价格原始": "varchar", "期货合约": "varchar", "基差": "decimal", "现货价": "decimal",
    "成交量": "decimal", "created_at": "datetime", "报价类别": "varchar", "期货收盘价": "decimal",
}
LUTOU_DOMESTIC_BASIS_NOT_NULL = frozenset({"日期", "品种", "文章ID"})
_CONTRACT = re.compile(r"^\d{2}(0[1-9]|1[0-2])$")


class DomesticBasisEvidenceType(StrEnum):
    DETERMINISTIC_TEST_FIXTURE = "DETERMINISTIC_TEST_FIXTURE"
    LIVE_DATABASE = "LIVE_DATABASE"


@dataclass(frozen=True, slots=True)
class DomesticBasisSeries:
    series_id: str
    source_product: str
    consumer_product: str
    product: str
    region: str
    region_id: str
    underlying_exchange: Exchange
    underlying_product: str
    legacy_status: str
    live_status: str
    expected_source_status: str
    provider_dataset_id: str
    source_schema: str | None
    source_table: str

    @property
    def provider_series_id(self) -> str:
        return f"{self.provider_dataset_id}:{self.source_product}:{self.region}:现货基差"

    @property
    def source_series_id(self) -> str:
        return self.provider_series_id

    @property
    def source_locator(self) -> str:
        schema = self.source_schema or "LIVE_CONFIRMATION_PENDING"
        return f"database:lutou/schema:{schema}/relation:{self.source_table}"


@dataclass(frozen=True, slots=True)
class DomesticBasisCatalog:
    schema_version: str
    mapping_version: str
    provider: str
    source_contract: dict[str, object]
    canonical_policy: dict[str, object]
    series: tuple[DomesticBasisSeries, ...]
    freshness_policy: FreshnessPolicy = field(default_factory=lambda: FreshnessPolicy("domestic-basis-freshness/pending"))

    @property
    def live_verified(self) -> bool:
        return bool(self.series) and all(x.live_status == "LIVE_CONFIRMED" for x in self.series)

    @property
    def source_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((x.source_product, x.region) for x in self.series)

    def match(self, source_product: str, region: str) -> DomesticBasisSeries:
        found = [x for x in self.series if x.source_product == source_product and x.region == region]
        if len(found) != 1:
            raise ValueError(f"Domestic Basis source mapping is not unique for {source_product}/{region}")
        return found[0]


def _decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True, slots=True)
class DomesticBasisSourceRow:
    """One raw source row; null and invalid observations remain auditable."""

    business_date: date
    source_product: str
    region: str
    source_quote_type: str | None
    source_contract_code: str | None
    basis_value: Decimal | None
    source_row_identity: str
    source_identity_sha256: str | None = None
    source_row_sha256: str | None = None
    raw_basis_value: str | None = None
    cash_price: Decimal | None = None
    futures_price: Decimal | None = None
    factory: str | None = None
    article_id: str | None = None
    article_title: str | None = None
    row_type: str | None = None
    province: str | None = None
    contract_situation: str | None = None
    contract_year_native: str | None = None
    delivery_month_native: str | None = None
    raw_price_text: str | None = None
    volume: Decimal | None = None
    source_created_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise TypeError("business_date must be an exact date")
        for field_name in ("source_product", "region", "source_row_identity"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        for field_name in ("basis_value", "cash_price", "futures_price", "volume"):
            value = getattr(self, field_name)
            parsed = _decimal(value)
            if value is not None and parsed is None:
                raise ValueError(f"{field_name} must be finite when parsed")
            object.__setattr__(self, field_name, parsed)
        if self.source_identity_sha256 is None:
            object.__setattr__(self, "source_identity_sha256", hashlib.sha256(self.source_row_identity.encode("utf-8")).hexdigest())
        if self.source_row_sha256 is None:
            object.__setattr__(self, "source_row_sha256", hashlib.sha256(self.source_row_identity.encode("utf-8")).hexdigest())
        for field_name in ("source_identity_sha256", "source_row_sha256"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256")

    @property
    def contract_is_parseable(self) -> bool:
        return isinstance(self.source_contract_code, str) and _CONTRACT.fullmatch(self.source_contract_code) is not None


@dataclass(frozen=True, slots=True)
class DomesticBasisSourceInventory:
    source_product: str
    region: str
    source_latest_date: date | None
    window_row_count: int

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (self.source_product, self.region)):
            raise ValueError("Domestic Basis inventory identity is invalid")
        if self.source_latest_date is not None and type(self.source_latest_date) is not date:
            raise ValueError("Domestic Basis inventory latest must be an exact date or null")
        if type(self.window_row_count) is not int or self.window_row_count < 0:
            raise ValueError("Domestic Basis inventory count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class DomesticBasisExtraction:
    records: tuple[DomesticBasisSourceRow, ...]
    evidence_type: DomesticBasisEvidenceType
    query_identity: str
    snapshot_identity: str
    extracted_at: datetime
    source_min_date: date | None = None
    source_max_date: date | None = None
    plan_estimated_rows: int | None = None
    connection_proof: Mapping[str, object] | None = None
    schema_proof: Mapping[str, object] | None = None
    partition_count: int = 1
    partition_windows: tuple[tuple[date, date], ...] = ()
    source_inventory: tuple[DomesticBasisSourceInventory, ...] = ()
    inventory_query_identity: str | None = None
    inventory_plan_estimated_rows: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_type, DomesticBasisEvidenceType):
            raise TypeError("evidence_type must be DomesticBasisEvidenceType")
        # An empty increment is legal only with independently verified inventory;
        # full/offline coverage gates still reject an empty dataset.
        if not self.records and not self.source_inventory:
            raise ValueError("Empty Domestic Basis extraction requires source inventory")
        if not self.query_identity.strip() or not self.snapshot_identity.strip():
            raise ValueError("Domestic Basis extraction identities must be non-empty")
        if self.extracted_at.tzinfo is None or self.extracted_at.utcoffset() is None:
            raise ValueError("extracted_at must be timezone-aware")


class DomesticBasisSourceAdapter(Protocol):
    def date_bounds(self) -> tuple[date, date]: ...
    def extract(self, *, catalog: DomesticBasisCatalog, start_date: date, end_date: date) -> DomesticBasisExtraction: ...


class LutouDomesticBasisLiveAdapter:
    """Date-bounded SELECT through the existing fail-closed read-only client."""

    def __init__(
        self,
        client: LutouClient,
        *,
        partition_days: int | None = None,
        progress_callback: Callable[[int, date, date, int], None] | None = None,
    ) -> None:
        if partition_days is not None and partition_days <= 0:
            raise ValueError("Domestic Basis partition_days must be positive")
        self._client = client
        self._partition_days = partition_days
        self._progress_callback = progress_callback
        self.query = LutouQuery(
            schema=LUTOU_DOMESTIC_BASIS_SCHEMA,
            table=LUTOU_DOMESTIC_BASIS_TABLE,
            date_column="日期",
            value_columns=LUTOU_DOMESTIC_BASIS_COLUMNS,
            version="domestic-basis-live/1",
            max_plan_rows=200_000,
            max_window_days=20_000,
            order_by_date=False,
        )

    def inspect_schema(self) -> tuple[dict[str, object], ...]:
        return self._client.inspect_relation(self.query.schema, self.query.table)

    def inspect_indexes(self) -> tuple[dict[str, object], ...]:
        return self._client.inspect_relation_indexes(self.query.schema, self.query.table)

    def verify_schema(self) -> dict[str, object]:
        columns = self.inspect_schema()
        actual = {str(item["COLUMN_NAME"]): str(item["DATA_TYPE"]).lower() for item in columns}
        if actual != LUTOU_DOMESTIC_BASIS_TYPES:
            raise LutouSchemaError("Domestic Basis live column types differ from the sealed contract")
        actual_not_null = {str(item["COLUMN_NAME"]) for item in columns if str(item["IS_NULLABLE"]).upper() == "NO"}
        if actual_not_null != LUTOU_DOMESTIC_BASIS_NOT_NULL:
            raise LutouSchemaError("Domestic Basis live nullability differs from the sealed contract")
        indexes = self.inspect_indexes()
        if not any(str(item.get("COLUMN_NAME")) == "日期" for item in indexes):
            raise LutouSchemaError("Domestic Basis live date column has no approved index")
        return {"column_count": len(columns), "index_entry_count": len(indexes), "date_indexed": True}

    def date_bounds(self) -> tuple[date, date]:
        return self._client.date_bounds(self.query)

    def extract(self, *, catalog: DomesticBasisCatalog, start_date: date, end_date: date) -> DomesticBasisExtraction:
        schema_proof = self.verify_schema()
        self._client.inspect_query(self.query)
        inventory = self._client.series_inventory(self.query, ("品种", "地区"), start_date, end_date)
        inventories: dict[tuple[str, str], DomesticBasisSourceInventory] = {}
        for item in inventory["rows"]:
            pair = (_optional_text(item.get("品种")), _optional_text(item.get("地区")))
            if pair not in catalog.source_pairs:
                continue
            latest = item["source_latest_date"]
            count = item["window_row_count"]
            if type(latest) is not date or latest > end_date or int(count) != count or count < 0:
                raise ValueError("Domestic Basis source inventory is invalid")
            # SQL groups untrimmed text; normalized aliases are combined exactly
            # as the existing source-pair filter does, not silently overwritten.
            before = inventories.get(pair)
            inventories[pair] = DomesticBasisSourceInventory(
                *pair, max(latest, before.source_latest_date) if before else latest,
                int(count) + (before.window_row_count if before else 0),
            )
        source_inventory = tuple(
            inventories.get(pair, DomesticBasisSourceInventory(*pair, None, 0))
            for pair in sorted(catalog.source_pairs)
        )
        raw_rows: list[dict[str, object]] = []
        plan_rows = 0
        windows: list[tuple[date, date]] = []
        partition_start = start_date
        partition_days = self._partition_days
        if partition_days is None:
            partition_days = 1 if (end_date - start_date).days <= 32 else 92
        while partition_start <= end_date:
            partition_end = min(
                end_date,
                partition_start + timedelta(days=partition_days - 1),
            )
            plan, batches = self._client.plan_stream(
                self.query, partition_start, partition_end, batch_size=5_000
            )
            plan_rows += plan.estimated_rows
            windows.append((partition_start, partition_end))
            raw_rows.extend(
                dict(row)
                for batch in batches
                for row in batch.rows
                if (
                    _optional_text(row.get("品种")),
                    _optional_text(row.get("地区")),
                ) in catalog.source_pairs
            )
            if self._progress_callback is not None:
                self._progress_callback(len(windows), partition_start, partition_end, len(raw_rows))
            partition_start = partition_end + timedelta(days=1)
        observed = Counter((_optional_text(row.get("品种")), _optional_text(row.get("地区"))) for row in raw_rows)
        if any(observed[(item.source_product, item.region)] != item.window_row_count for item in source_inventory):
            raise ValueError("Domestic Basis source extraction/inventory count mismatch")
        identity_fields = catalog.source_contract.get("source_row_identity_fields")
        if not isinstance(identity_fields, list) or not identity_fields:
            raise ValueError("Domestic Basis configured source identity fields are missing")
        normalized = [
            (_row_sha({str(key): x.get(str(key)) for key in identity_fields}), _row_sha(x), _canonical_json(x), x)
            for x in raw_rows
        ]
        normalized.sort(key=lambda item: (item[0], item[1], item[2]))
        occurrences: Counter[str] = Counter()
        records: list[DomesticBasisSourceRow] = []
        for identity_sha, row_sha, _, raw in normalized:
            occurrences[identity_sha] += 1
            business_date = raw.get("日期")
            if isinstance(business_date, datetime):
                business_date = business_date.date()
            if type(business_date) is not date:
                raise ValueError("Domestic Basis live business date is invalid")
            if not start_date <= business_date <= end_date:
                raise ValueError("Domestic Basis source row is outside extraction window")
            raw_basis = raw.get("基差")
            records.append(DomesticBasisSourceRow(
                business_date=business_date,
                source_product=_required_text(raw.get("品种")), region=_required_text(raw.get("地区")),
                source_quote_type=_optional_text(raw.get("报价类别")),
                source_contract_code=_optional_text(raw.get("期货合约")),
                basis_value=_decimal(raw_basis), raw_basis_value=None if raw_basis is None else str(raw_basis),
                source_identity_sha256=identity_sha, source_row_sha256=row_sha,
                source_row_identity=f"{identity_sha}:{occurrences[identity_sha]}",
                cash_price=_decimal(raw.get("现货价")), futures_price=_decimal(raw.get("期货收盘价")),
                factory=_optional_text(raw.get("工厂")), article_id=_optional_text(raw.get("文章ID")),
                article_title=_optional_text(raw.get("文章标题")), row_type=_optional_text(raw.get("行类型")),
                province=_optional_text(raw.get("省份")), contract_situation=_optional_text(raw.get("合同情况")),
                contract_year_native=_optional_text(raw.get("基差合同年")),
                delivery_month_native=_optional_text(raw.get("基差合同月")),
                raw_price_text=_optional_text(raw.get("价格原始")), volume=_decimal(raw.get("成交量")),
                source_created_at=raw.get("created_at") if isinstance(raw.get("created_at"), datetime) else None,
            ))
        snapshot = hashlib.sha256(
            "\n".join(
                f"{x.source_row_identity}|{x.source_row_sha256}" for x in records
            ).encode("utf-8")
        ).hexdigest()
        return DomesticBasisExtraction(
            records=tuple(records), evidence_type=DomesticBasisEvidenceType.LIVE_DATABASE,
            query_identity=self.query.sha256, snapshot_identity=snapshot,
            extracted_at=datetime.now(timezone.utc), source_min_date=start_date,
            source_max_date=end_date, plan_estimated_rows=plan_rows,
            connection_proof=self._client.proof.safe_manifest_fields(),
            schema_proof=schema_proof,
            partition_count=len(windows), partition_windows=tuple(windows),
            source_inventory=source_inventory,
            inventory_query_identity=str(inventory["query_identity"]),
            inventory_plan_estimated_rows=int(inventory["plan_estimated_rows"]),
        )


def _required_text(value: object) -> str:
    result = _optional_text(value)
    if result is None:
        raise ValueError("Domestic Basis required source text is empty")
    return result


def _optional_text(value: object) -> str | None:
    result = "" if value is None else str(value).strip()
    return result or None


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _canonical_json(row: Mapping[str, object]) -> str:
    return json.dumps({str(k): _json_value(v) for k, v in sorted(row.items())}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_sha(row: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(row).encode("utf-8")).hexdigest()


def load_domestic_basis_catalog(path: str | Path = DEFAULT_MAPPING_PATH) -> DomesticBasisCatalog:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Domestic Basis mapping must be a YAML object")
    source, policy, rows = payload.get("source_contract"), payload.get("canonical_policy"), payload.get("series")
    if not isinstance(source, dict) or not isinstance(policy, dict) or not isinstance(rows, list):
        raise ValueError("Domestic Basis mapping sections are incomplete")
    table_status = str(source.get("table_status", ""))
    provider_dataset_id = str(source.get("provider_dataset_id_expected", ""))
    source_schema = None if source.get("database_schema") is None else str(source["database_schema"])
    table = str(source.get("table", ""))
    series = tuple(DomesticBasisSeries(
        series_id=str(x["series_id"]), source_product=str(x["source_product"]),
        consumer_product=str(x["consumer_product"]), product=str(x["product"]),
        region=str(x["region"]), region_id=str(x["region_id"]),
        underlying_exchange=Exchange(str(x["underlying_exchange"])), underlying_product=str(x["underlying_product"]),
        legacy_status=str(x["legacy_status"]), live_status=str(x["live_status"]),
        expected_source_status=str(x["expected_source_status"]), provider_dataset_id=provider_dataset_id,
        source_schema=source_schema, source_table=table,
    ) for x in rows)
    if not series or len({x.series_id for x in series}) != len(series) or len({(x.source_product, x.region) for x in series}) != len(series):
        raise ValueError("Domestic Basis mapping identities are empty or duplicated")
    if any(x.legacy_status != "LEGACY_IDENTIFIED" for x in series):
        raise ValueError("Domestic Basis legacy identity is incomplete")
    live_statuses, expected_statuses = {x.live_status for x in series}, {x.expected_source_status for x in series}
    if live_statuses not in ({"LIVE_CONFIRMATION_PENDING"}, {"LIVE_CONFIRMED"}):
        raise ValueError("Domestic Basis live verification status is inconsistent")
    if expected_statuses not in ({"EXPECTED_FROM_LEGACY_CONTRACT"}, {"LIVE_CONFIRMED"}):
        raise ValueError("Domestic Basis source expectation status is invalid")
    if live_statuses == {"LIVE_CONFIRMATION_PENDING"}:
        if table_status != "EXPECTED_FROM_LEGACY_CONTRACT" or source_schema is not None:
            raise ValueError("Pending Domestic Basis mapping must not claim live schema proof")
    elif table_status != "LIVE_CONFIRMED" or source_schema != LUTOU_DOMESTIC_BASIS_SCHEMA or table != LUTOU_DOMESTIC_BASIS_TABLE:
        raise ValueError("Live-confirmed Domestic Basis mapping requires the proven relation")
    if policy.get("promotion_requires") != "LIVE_CONFIRMED":
        raise ValueError("Domestic Basis promotion policy must require LIVE_CONFIRMED")
    freshness = FreshnessPolicy.from_mapping(payload.get("freshness_policy", {"policy_version": "domestic-basis-freshness/pending"}))
    return DomesticBasisCatalog(str(payload.get("schema_version", "")), str(payload.get("mapping_version", "")), str(payload.get("provider", "")), dict(source), dict(policy), series, freshness)


__all__ = [
    "DEFAULT_MAPPING_PATH", "LUTOU_DOMESTIC_BASIS_COLUMNS", "LUTOU_DOMESTIC_BASIS_NOT_NULL",
    "LUTOU_DOMESTIC_BASIS_SCHEMA", "LUTOU_DOMESTIC_BASIS_TYPES",
    "LUTOU_DOMESTIC_BASIS_TABLE", "DomesticBasisCatalog", "DomesticBasisEvidenceType",
    "DomesticBasisExtraction", "DomesticBasisSeries", "DomesticBasisSourceAdapter",
    "DomesticBasisSourceRow", "LutouDomesticBasisLiveAdapter", "load_domestic_basis_catalog",
]

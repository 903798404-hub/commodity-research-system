"""Domestic Spread historical price and publication integrity contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from agri_research_agent.data_sources.tankan.domestic_spread import (
    contract_code_from_source_column,
    normalize_full_contract_code,
    resolve_contract_season,
    window_contract_code,
)


HISTORICAL_KEY_COLUMNS = ("date", "spread_name", "season")
HISTORICAL_METADATA_COLUMNS = frozenset({"updated_at"})
CONTRACT_IDENTITY_COLUMNS = frozenset({"leg1_contract", "leg2_contract"})
UnderlyingPriceKey = tuple[str, str]
HistoricalSpreadKey = tuple[str, str, str]


class PriceSemantic(StrEnum):
    DAILY_CLOSE = "DAILY_CLOSE"
    INTRADAY_CURRENT_PRICE = "INTRADAY_CURRENT_PRICE"
    UNKNOWN = "UNKNOWN"


class HistoricalPublicationMode(StrEnum):
    NORMAL = "NORMAL"
    HISTORICAL_RECONCILIATION = "HISTORICAL_RECONCILIATION"


class HistoricalPublicationBlocked(RuntimeError):
    """Candidate contains a business mutation outside the declared boundary."""

    def __init__(self, report: "HistoricalDiffReport") -> None:
        self.report = report
        keys = ",".join("|".join(key) for key in report.blocked_keys)
        super().__init__(f"Domestic Spread historical publication blocked: {keys}")


@dataclass(frozen=True, slots=True)
class HistoricalMutationPolicy:
    mode: HistoricalPublicationMode
    start_date: date
    end_date: date
    allowed_keys: frozenset[tuple[str, str, str]] = frozenset()
    exact_keys: frozenset[HistoricalSpreadKey] | None = None

    def __post_init__(self) -> None:
        if self.mode is HistoricalPublicationMode.NORMAL and self.allowed_keys:
            raise ValueError("normal refresh cannot declare historical allowed keys")
        if (
            self.mode is HistoricalPublicationMode.HISTORICAL_RECONCILIATION
            and not self.allowed_keys
        ):
            raise ValueError("historical reconciliation requires exact allowed keys")


@dataclass(frozen=True, slots=True)
class HistoricalDiffReport:
    mode: str
    changed_keys: tuple[tuple[str, str, str], ...]
    blocked_keys: tuple[tuple[str, str, str], ...]

    @property
    def publication_allowed(self) -> bool:
        return not self.blocked_keys

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["publication_allowed"] = self.publication_allowed
        return payload


@dataclass(frozen=True, slots=True)
class IncrementalMaterializationReport:
    underlying_keys: tuple[UnderlyingPriceKey, ...]
    affected_keys: tuple[HistoricalSpreadKey, ...]
    replaced_keys: tuple[HistoricalSpreadKey, ...]
    appended_keys: tuple[HistoricalSpreadKey, ...]
    unrelated_rewritten_keys: tuple[HistoricalSpreadKey, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_price_semantic(source_file: object, source_column: object) -> PriceSemantic:
    """Classify only source/field pairs with an explicit historical meaning."""

    source = str(source_file).strip().lower()
    column = str(source_column).strip().lower()
    field = column.rsplit(":", 1)[-1]
    source_name = source.replace("\\", "/").rsplit("/", 1)[-1]
    if source_name == "historical_price_base.xlsx":
        # The approved imported base is the pre-existing canonical daily-close
        # history.  Its legacy column labels predate explicit semantic fields.
        return PriceSemantic.DAILY_CLOSE
    if source == "akshare_futures_zh_daily_sina" and field == "close":
        return PriceSemantic.DAILY_CLOSE
    if source == "tankan.market.futures_spread" and field == "close_price":
        return PriceSemantic.DAILY_CLOSE
    if source == "akshare_futures_zh_spot" and field == "current_price":
        return PriceSemantic.INTRADAY_CURRENT_PRICE
    return PriceSemantic.UNKNOWN


def select_canonical_historical_prices(price_long: pd.DataFrame) -> pd.DataFrame:
    """Return one DAILY_CLOSE row per historical contract/date identity.

    A provenance-free frame is treated as an already-normalized internal frame
    for backwards-compatible pure calculation tests.  Formal price_long inputs
    carry both provenance columns and are filtered strictly.
    """

    frame = price_long.copy()
    provenance = {"source_file", "source_column"}
    if provenance <= set(frame.columns):
        frame["price_semantic"] = [
            infer_price_semantic(source, column).value
            for source, column in zip(
                frame["source_file"], frame["source_column"], strict=True
            )
        ]
        frame = frame.loc[
            frame["price_semantic"].eq(PriceSemantic.DAILY_CLOSE.value)
        ].copy()
    keys = ["date", "instrument", "delivery_month"]
    if not set(keys) <= set(frame.columns):
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if "updated_at" in frame.columns:
        frame["_integrity_updated_at"] = pd.to_datetime(
            frame["updated_at"], errors="coerce"
        )
        frame = frame.sort_values(keys + ["_integrity_updated_at"], na_position="first")
        frame = frame.drop(columns="_integrity_updated_at")
    else:
        frame = frame.sort_values(keys)
    return frame.drop_duplicates(keys, keep="last").reset_index(drop=True)


def changed_daily_close_keys(
    current: pd.DataFrame | str | Path,
    candidate: pd.DataFrame | str | Path,
) -> frozenset[UnderlyingPriceKey]:
    """Return exact changed ``(full_contract, trade_date)`` daily-close keys.

    Rows without an exact full-contract identity remain supported as immutable
    legacy history. If any such row changes, the incremental boundary cannot
    be proven and the operation fails closed.
    """

    before_exact, before_legacy = _daily_close_rows(_read_prices(current))
    after_exact, after_legacy = _daily_close_rows(_read_prices(candidate))
    if before_legacy != after_legacy:
        raise ValueError(
            "Domestic Spread changed daily-close row lacks full contract identity"
        )
    return frozenset(
        key
        for key in set(before_exact) | set(after_exact)
        if before_exact.get(key) != after_exact.get(key)
    )


def derive_affected_spread_keys(
    config: pd.DataFrame,
    underlying_keys: Iterable[UnderlyingPriceKey],
) -> frozenset[HistoricalSpreadKey]:
    """Resolve the config-driven spread dependency closure for exact inputs."""

    required = {
        "spread_name",
        "leg1_instrument",
        "leg1_month",
        "leg2_instrument",
        "leg2_month",
        "window_start_month",
        "window_start_day",
        "window_end_month",
        "window_end_day",
    }
    missing = sorted(required - set(config.columns))
    if missing:
        raise ValueError(
            f"Domestic Spread dependency config is missing: {','.join(missing)}"
        )
    active = config.copy()
    if "enabled" in active.columns:
        active = active.loc[active["enabled"].map(_is_enabled)].copy()

    affected: set[HistoricalSpreadKey] = set()
    for raw_contract, raw_date in underlying_keys:
        contract = normalize_full_contract_code(raw_contract)
        business_date = date.fromisoformat(str(raw_date))
        for row in active.to_dict("records"):
            season = resolve_contract_season(
                business_date,
                window_start_month=int(row["window_start_month"]),
                window_start_day=int(row["window_start_day"]),
                window_end_month=int(row["window_end_month"]),
                window_end_day=int(row["window_end_day"]),
            )
            if season is None:
                continue
            dependencies = {
                window_contract_code(
                    str(row["leg1_instrument"]),
                    season.label,
                    int(row["leg1_month"]),
                    window_start_month=int(row["window_start_month"]),
                ),
                window_contract_code(
                    str(row["leg2_instrument"]),
                    season.label,
                    int(row["leg2_month"]),
                    window_start_month=int(row["window_start_month"]),
                ),
            }
            if contract in dependencies:
                affected.add(
                    (business_date.isoformat(), str(row["spread_name"]), season.label)
                )
    return frozenset(affected)


def materialize_affected_spreads(
    current: pd.DataFrame | str | Path,
    full_recalculation: pd.DataFrame | str | Path,
    *,
    underlying_keys: Iterable[UnderlyingPriceKey],
    affected_keys: Iterable[HistoricalSpreadKey],
) -> tuple[pd.DataFrame, IncrementalMaterializationReport]:
    """Upsert only the declared dependency closure into pinned Current."""

    before = _read_spreads(current)
    calculated = _read_spreads(full_recalculation)
    # Old formal artifacts lack both identities. Preserve those unknown values
    # as NULL; only the exact affected keys receive actual producer identities.
    added = set(calculated.columns) - set(before.columns)
    if not set(before.columns) - set(calculated.columns) and added <= CONTRACT_IDENTITY_COLUMNS:
        for column in added:
            before[column] = None
    if set(before.columns) != set(calculated.columns):
        raise ValueError("Domestic Spread incremental schemas do not match")
    calculated = calculated.loc[:, before.columns]
    before_rows = _business_rows(before)
    calculated_rows = _business_rows(calculated)
    affected = frozenset(_normalize_spread_key(key) for key in affected_keys)
    missing = sorted(affected - set(calculated_rows))
    if missing:
        encoded = ",".join("|".join(key) for key in missing)
        raise ValueError(
            f"Domestic Spread calculator omitted affected key(s): {encoded}"
        )

    before_keys = _frame_spread_keys(before)
    calculated_keys = _frame_spread_keys(calculated)
    calculated_by_key = {
        key: record
        for key, record in zip(
            calculated_keys, calculated.to_dict("records"), strict=True
        )
        if key in affected
    }
    output_records: list[dict[str, Any]] = []
    consumed: set[HistoricalSpreadKey] = set()
    for key, record in zip(before_keys, before.to_dict("records"), strict=True):
        if key in affected:
            output_records.append(calculated_by_key[key])
            consumed.add(key)
        else:
            output_records.append(record)
    for key in sorted(affected - consumed):
        output_records.append(calculated_by_key[key])
    result = pd.DataFrame.from_records(output_records, columns=before.columns)
    if not result.empty:
        result["date"] = pd.to_datetime(result["date"], errors="raise")

    after_rows = _business_rows(result)
    unrelated_rewritten = tuple(
        sorted(
            key
            for key in (set(before_rows) | set(after_rows)) - affected
            if before_rows.get(key) != after_rows.get(key)
        )
    )
    if unrelated_rewritten:
        raise ValueError("Domestic Spread incremental merge rewrote unrelated rows")
    replaced = tuple(sorted(affected & set(before_rows)))
    appended = tuple(sorted(affected - set(before_rows)))
    report = IncrementalMaterializationReport(
        tuple(sorted(_normalize_underlying_key(key) for key in underlying_keys)),
        tuple(sorted(affected)),
        replaced,
        appended,
        unrelated_rewritten,
    )
    return result, report


def historical_changed_keys(
    current: pd.DataFrame | str | Path,
    candidate: pd.DataFrame | str | Path,
) -> tuple[HistoricalSpreadKey, ...]:
    """Return business-key changes without granting publication permission."""

    before, after = _align_legacy_contract_columns(_read_spreads(current), _read_spreads(candidate))
    before_rows = _business_rows(before)
    after_rows = _business_rows(after)
    return tuple(
        sorted(
            key
            for key in set(before_rows) | set(after_rows)
            if before_rows.get(key) != after_rows.get(key)
        )
    )


def validate_historical_publication(
    current: pd.DataFrame | str | Path,
    candidate: pd.DataFrame | str | Path,
    policy: HistoricalMutationPolicy,
) -> HistoricalDiffReport:
    """Compare business rows and enforce the normal/reconciliation boundary."""

    before = _read_spreads(current)
    after = _read_spreads(candidate)
    before, after = _align_legacy_contract_columns(before, after)
    before_rows = _business_rows(before)
    after_rows = _business_rows(after)
    changed: list[tuple[str, str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    for key in sorted(set(before_rows) | set(after_rows)):
        if before_rows.get(key) == after_rows.get(key):
            continue
        changed.append(key)
        business_date = date.fromisoformat(key[0])
        in_requested_range = policy.start_date <= business_date <= policy.end_date
        if policy.exact_keys is not None:
            allowed = key in policy.exact_keys or (
                policy.mode is HistoricalPublicationMode.HISTORICAL_RECONCILIATION
                and key in policy.allowed_keys
            )
        else:
            allowed = in_requested_range or (
                policy.mode is HistoricalPublicationMode.HISTORICAL_RECONCILIATION
                and key in policy.allowed_keys
            )
        if not allowed:
            blocked.append(key)
    report = HistoricalDiffReport(
        policy.mode.value, tuple(changed), tuple(blocked)
    )
    if not report.publication_allowed:
        raise HistoricalPublicationBlocked(report)
    return report


def parse_allowed_key(value: str) -> tuple[str, str, str]:
    parts = tuple(part.strip() for part in value.split("|"))
    if len(parts) != 3 or not all(parts):
        raise ValueError("allowed key must be DATE|SPREAD_NAME|SEASON")
    date.fromisoformat(parts[0])
    return parts


def _align_legacy_contract_columns(
    before: pd.DataFrame, after: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Absent legacy identity means NULL, never a guessed listed contract.

    Populating an old NULL with a real identity remains a business mutation and
    must pass the exact-key publication guard. No other schema change is hidden.
    """
    before, after = before.copy(), after.copy()
    for column in CONTRACT_IDENTITY_COLUMNS & (set(before.columns) | set(after.columns)):
        if column not in before:
            before[column] = None
        if column not in after:
            after[column] = None
    return before, after


def _read_spreads(value: pd.DataFrame | str | Path) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    path = Path(value)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_excel(path, sheet_name="spread_long")


def _read_prices(value: pd.DataFrame | str | Path) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    return pd.read_excel(Path(value), sheet_name="price_long")


def _daily_close_rows(
    frame: pd.DataFrame,
) -> tuple[
    dict[UnderlyingPriceKey, tuple[Any, ...]],
    dict[tuple[str, ...], tuple[Any, ...]],
]:
    required = {
        "date",
        "instrument",
        "delivery_month",
        "price",
        "source_file",
        "source_column",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(
            f"Domestic Spread price diff schema is missing: {','.join(missing)}"
        )
    exact: dict[UnderlyingPriceKey, tuple[Any, ...]] = {}
    legacy: dict[tuple[str, ...], tuple[Any, ...]] = {}
    for row in frame.to_dict("records"):
        if infer_price_semantic(
            row.get("source_file"), row.get("source_column")
        ) is not PriceSemantic.DAILY_CLOSE:
            continue
        raw_date = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.isna(raw_date):
            raise ValueError("Domestic Spread price diff contains an invalid date")
        date_text = pd.Timestamp(raw_date).date().isoformat()
        instrument = str(row.get("instrument", "")).strip().upper()
        month = int(row.get("delivery_month"))
        contract = contract_code_from_source_column(
            row.get("source_column"),
            instrument=instrument,
            delivery_month=month,
        )
        value = (
            _stable_value(row.get("price")),
            str(row.get("status", "")),
            str(row.get("error", "")),
            str(row.get("source_file", "")),
            str(row.get("source_column", "")),
        )
        if contract is not None:
            key = (contract, date_text)
            if key in exact:
                raise ValueError(
                    f"Domestic Spread daily-close diff contains duplicate key: {contract}|{date_text}"
                )
            exact[key] = value
        else:
            key = (
                date_text,
                instrument,
                str(month),
                str(row.get("source_file", "")),
                str(row.get("source_column", "")),
            )
            if key in legacy:
                raise ValueError(
                    "Domestic Spread legacy daily-close diff contains duplicate key"
                )
            legacy[key] = value
    return exact, legacy


def _frame_spread_keys(frame: pd.DataFrame) -> list[HistoricalSpreadKey]:
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise ValueError("Domestic Spread diff contains an invalid date")
    return [
        (
            pd.Timestamp(row.date).date().isoformat(),
            str(row.spread_name),
            str(row.season),
        )
        for row in normalized[["date", "spread_name", "season"]].itertuples(
            index=False
        )
    ]


def _normalize_underlying_key(value: UnderlyingPriceKey) -> UnderlyingPriceKey:
    contract, business_date = value
    return (
        normalize_full_contract_code(contract),
        date.fromisoformat(str(business_date)).isoformat(),
    )


def _normalize_spread_key(value: HistoricalSpreadKey) -> HistoricalSpreadKey:
    business_date, spread_name, season = value
    return (
        date.fromisoformat(str(business_date)).isoformat(),
        str(spread_name),
        str(season),
    )


def _is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是", "启用"}


def _business_rows(frame: pd.DataFrame) -> dict[tuple[str, str, str], tuple[Any, ...]]:
    missing = set(HISTORICAL_KEY_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            f"Domestic Spread diff schema is missing: {','.join(sorted(missing))}"
        )
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise ValueError("Domestic Spread diff contains an invalid date")
    value_columns = sorted(
        column
        for column in normalized.columns
        if column not in HISTORICAL_METADATA_COLUMNS
        and column not in HISTORICAL_KEY_COLUMNS
    )
    rows: dict[tuple[str, str, str], tuple[Any, ...]] = {}
    for record in normalized.to_dict("records"):
        key = (
            pd.Timestamp(record["date"]).date().isoformat(),
            str(record["spread_name"]),
            str(record["season"]),
        )
        if key in rows:
            raise ValueError(f"Domestic Spread diff contains duplicate key: {'|'.join(key)}")
        rows[key] = tuple(_stable_value(record.get(column)) for column in value_columns)
    return rows


def _stable_value(value: object) -> object:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 10)
    return value


__all__ = [
    "HistoricalDiffReport", "HistoricalMutationPolicy",
    "HistoricalPublicationBlocked", "HistoricalPublicationMode",
    "HistoricalSpreadKey", "IncrementalMaterializationReport",
    "PriceSemantic", "UnderlyingPriceKey", "changed_daily_close_keys",
    "derive_affected_spread_keys", "historical_changed_keys",
    "infer_price_semantic", "materialize_affected_spreads", "parse_allowed_key",
    "select_canonical_historical_prices", "validate_historical_publication",
]

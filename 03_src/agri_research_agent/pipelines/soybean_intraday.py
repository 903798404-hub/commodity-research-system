"""Materialize AM/PM soybean profit from sealed Public market data and daily CNF."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from math import isfinite
from numbers import Real
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Mapping
import uuid

from agri_research_agent.import_profit.cnf_store import (
    CnfQuoteUpdate,
    CnfWriteResult,
    load_cnf_store,
    upsert_cnf_quotes,
)
from agri_research_agent.import_profit.config import SoybeanImportProfitConfig
from agri_research_agent.import_profit.daily_increment import generate_daily_business_keys
from agri_research_agent.import_profit.intraday import (
    SoybeanIntradayCnfError,
    calculate_soybean_intraday_profit,
    load_soybean_intraday_market_inputs,
)
from agri_research_agent.import_profit.intraday_store import (
    IntradayProfitSealResult,
    IntradayProfitStoreNotFoundError,
    SoybeanIntradayResultBatch,
    load_intraday_profit_batch,
    seal_intraday_profit_batch,
)
from agri_research_agent.import_profit.historical_cnf_adapter import (
    shipment_year_for,
)
from agri_research_agent.import_profit.models import BusinessKey
from agri_research_agent.market_data.intraday import (
    MarketSession,
    load_intraday_snapshot,
)


@dataclass(frozen=True, slots=True)
class SoybeanIntradayCnfSaveReceipt:
    business_date: date
    cnf_record_count: int
    cnf_sha256: str
    coverage_by_origin: Mapping[str, tuple[int, ...]]
    write_result: CnfWriteResult


@dataclass(frozen=True, slots=True)
class SoybeanAmClosureReceipt:
    cnf: SoybeanIntradayCnfSaveReceipt
    materialize: IntradayProfitSealResult
    am_record_count: int
    calculable_periods: tuple[str, ...]
    unavailable_periods: tuple[tuple[str, str], ...]
    snapshot_release_id: str
    snapshot_captured_at: datetime
    snapshot_content_sha256_before: str
    snapshot_content_sha256_after: str
    snapshot_quotes_sha256_before: str
    snapshot_quotes_sha256_after: str
    snapshot_immutability_pass: bool


def save_soybean_intraday_manual_cnf(
    *,
    cnf_store_path: str | Path,
    business_date: date,
    values: Mapping[tuple[str, int], float | None],
    config: SoybeanImportProfitConfig,
    updated_at: datetime,
) -> SoybeanIntradayCnfSaveReceipt:
    """Validate and atomically persist user-entered daily ``manual_ui`` CNF."""

    expected_keys = {
        (origin, month)
        for origin in config.origin_codes
        for month in range(1, 13)
    }
    if set(values) != expected_keys:
        raise ValueError("manual CNF payload must contain every origin/month field")
    if updated_at.tzinfo is None:
        raise ValueError("updated_at must be timezone-aware")
    normalized: dict[tuple[str, int], float | None] = {}
    for key, value in values.items():
        if value is None:
            normalized[key] = None
        elif isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("manual CNF values must be finite numbers or NULL")
        else:
            number = float(value)
            if not isfinite(number):
                raise ValueError("manual CNF values must be finite numbers or NULL")
            normalized[key] = number

    store_path = Path(cnf_store_path)
    current = load_cnf_store(store_path, allowed_origins=config.origin_codes)
    current_by_key = {
        (
            item.business_key.business_date,
            item.business_key.origin,
            item.business_key.shipment_month,
        ): item
        for item in current.records
    }
    batch_id = f"manual-ui-{business_date.isoformat()}-{uuid.uuid4().hex[:12]}"
    updates = []
    for (origin, month), value in sorted(normalized.items()):
        existing = current_by_key.get((business_date, origin, month))
        if existing is None and value is None:
            continue
        if existing is not None and existing.cnf_cents_per_bushel == value and existing.source == "manual_ui":
            continue
        updates.append(
            CnfQuoteUpdate(
                business_key=BusinessKey(
                    business_date=business_date,
                    commodity=config.commodity,
                    origin=origin,
                    shipment_year=shipment_year_for(business_date, month),
                    shipment_month=month,
                    allowed_origins=config.origin_codes,
                    expected_commodity=config.commodity,
                ),
                cnf_cents_per_bushel=value,
                source="manual_ui",
                updated_at=updated_at,
                batch_id=batch_id,
            )
        )
    if not updates:
        if not any(
            item.business_key.business_date == business_date
            for item in current.records
        ):
            raise ValueError("no manual CNF change was provided")
        write_result = CnfWriteResult(
            status="no_change",
            previous_sha256=current.store_sha256,
            new_sha256=current.store_sha256,
            previous_record_count=current.record_count,
            new_record_count=current.record_count,
            inserted_count=0,
            updated_count=0,
            cleared_to_null_count=0,
            unchanged_count=len(current.records),
            batch_id=batch_id,
            updated_keys=(),
            backup_created=False,
            backup_filename=None,
        )
        saved = current
    else:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        write_result = upsert_cnf_quotes(
            store_path,
            tuple(updates),
            allowed_origins=config.origin_codes,
            expected_store_sha256=current.store_sha256,
        )
        saved = load_cnf_store(
            store_path, allowed_origins=config.origin_codes
        )
    if saved.store_sha256 is None:
        raise RuntimeError("manual CNF store identity is missing after save")
    coverage = {
        origin: tuple(
            sorted(
                item.business_key.shipment_month
                for item in saved.records
                if item.business_key.business_date == business_date
                and item.business_key.origin == origin
                and item.cnf_cents_per_bushel is not None
            )
        )
        for origin in config.origin_codes
    }
    return SoybeanIntradayCnfSaveReceipt(
        business_date=business_date,
        cnf_record_count=saved.record_count,
        cnf_sha256=saved.store_sha256,
        coverage_by_origin=coverage,
        write_result=write_result,
    )


def save_manual_cnf_and_materialize_am(
    *,
    snapshot_root: str | Path,
    result_root: str | Path,
    cnf_store_path: str | Path,
    business_date: date,
    values: Mapping[tuple[str, int], float | None],
    config: SoybeanImportProfitConfig,
    saved_at: datetime | None = None,
) -> SoybeanAmClosureReceipt:
    """Save manual CNF, consume the existing SEALED AM, and prove immutability."""

    at = saved_at or datetime.now(timezone.utc)
    try:
        load_intraday_profit_batch(
            result_root, business_date, MarketSession.AM
        )
    except IntradayProfitStoreNotFoundError:
        pass
    else:
        raise RuntimeError(
            "AM result is already sealed; manual CNF cannot be changed"
        )
    before = _snapshot_identity(snapshot_root, business_date)

    # Exercise the complete CNF -> AM calculation path before touching the
    # formal CNF store.  This catches code/validation failures (including a
    # missing enum import) without leaving a formal partial write behind.
    formal_cnf_path = Path(cnf_store_path)
    with TemporaryDirectory(prefix="soybean-am-preflight-") as directory:
        preflight_root = Path(directory)
        preflight_cnf = preflight_root / "manual_cnf_quotes.parquet"
        if formal_cnf_path.is_file():
            shutil.copy2(formal_cnf_path, preflight_cnf)
        save_soybean_intraday_manual_cnf(
            cnf_store_path=preflight_cnf,
            business_date=business_date,
            values=values,
            config=config,
            updated_at=at,
        )
        materialize_soybean_intraday_profit(
            snapshot_root=str(snapshot_root),
            result_root=str(preflight_root / "results"),
            cnf_store_path=str(preflight_cnf),
            business_date=business_date,
            session=MarketSession.AM,
            config=config,
            calculated_at=at,
        )

    cnf = save_soybean_intraday_manual_cnf(
        cnf_store_path=cnf_store_path,
        business_date=business_date,
        values=values,
        config=config,
        updated_at=at,
    )
    materialized = materialize_soybean_intraday_profit(
        snapshot_root=str(snapshot_root),
        result_root=str(result_root),
        cnf_store_path=str(cnf_store_path),
        business_date=business_date,
        session=MarketSession.AM,
        config=config,
        calculated_at=at,
    )
    after = _snapshot_identity(snapshot_root, business_date)
    resolved = load_intraday_profit_batch(
        result_root, business_date, MarketSession.AM
    )
    calculable = []
    unavailable = []
    for row in resolved.rows:
        period = f"{int(row['shipment_year']):04d}-{int(row['shipment_month']):02d}"
        if row.get("availability_status") == "SUCCESS" and row.get(
            "calculation_status"
        ) == "success":
            calculable.append(period)
        else:
            reasons = row.get("missing_reasons") or [
                row.get("availability_status", "UNAVAILABLE")
            ]
            unavailable.append((period, ",".join(map(str, reasons))))
    immutable = before == after
    if not immutable:
        raise RuntimeError("SEALED AM snapshot identity changed during CNF materialization")
    return SoybeanAmClosureReceipt(
        cnf=cnf,
        materialize=materialized,
        am_record_count=len(resolved.rows),
        calculable_periods=tuple(calculable),
        unavailable_periods=tuple(unavailable),
        snapshot_release_id=before[0],
        snapshot_captured_at=before[1],
        snapshot_content_sha256_before=before[2],
        snapshot_content_sha256_after=after[2],
        snapshot_quotes_sha256_before=before[3],
        snapshot_quotes_sha256_after=after[3],
        snapshot_immutability_pass=True,
    )


def _snapshot_identity(
    snapshot_root: str | Path,
    business_date: date,
) -> tuple[str, datetime, str, str]:
    snapshot = load_intraday_snapshot(
        snapshot_root, business_date, MarketSession.AM
    )
    manifest_path = (
        Path(snapshot_root)
        / "releases"
        / snapshot.release_id
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return (
        snapshot.release_id,
        snapshot.captured_at,
        snapshot.content_sha256,
        str(manifest["quotes_sha256"]),
    )


def materialize_soybean_intraday_profit(
    *,
    snapshot_root: str,
    result_root: str,
    cnf_store_path: str,
    business_date: date,
    session: MarketSession,
    config: SoybeanImportProfitConfig,
    calculated_at: datetime,
) -> IntradayProfitSealResult:
    """Pure local consumer pipeline; it never opens a database connection."""

    cnf = load_cnf_store(cnf_store_path, allowed_origins=config.origin_codes)
    if not any(record.business_key.business_date == business_date and record.source == "manual_ui"
               for record in cnf.records):
        raise SoybeanIntradayCnfError("target business date has no saved manual_ui CNF")
    results = []
    for key in generate_daily_business_keys(business_date, config):
        inputs = load_soybean_intraday_market_inputs(
            snapshot_root,
            business_key=key,
            session=session,
            config=config,
        )
        results.append(
            calculate_soybean_intraday_profit(
                inputs,
                cnf_store=cnf,
                config=config,
                calculated_at=calculated_at,
            )
        )
    if not results:
        raise ValueError("intraday soybean request produced no business keys")
    first = results[0]
    batch = SoybeanIntradayResultBatch(
        business_date=business_date,
        session=session,
        market_snapshot_release_id=first.market_snapshot_release_id,
        market_snapshot_sha256=first.market_snapshot_sha256,
        market_captured_at=first.market_captured_at,
        cnf_identity=first.cnf_identity,
        calculated_at=calculated_at,
        results=tuple(results),
    )
    return seal_intraday_profit_batch(result_root, batch)


__all__ = [
    "SoybeanAmClosureReceipt",
    "SoybeanIntradayCnfSaveReceipt",
    "materialize_soybean_intraday_profit",
    "save_manual_cnf_and_materialize_am",
    "save_soybean_intraday_manual_cnf",
]

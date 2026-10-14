"""Incremental receipt candidates: revise returned days, preserve all other history."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from .model import FIELDS, DIMENSIONS, PRODUCTS, ReceiptError, aggregate_daily, load_snapshot, normalize_rows, sha256, write_snapshot
from .provider import acquire_history


def merge_observations(baseline: pd.DataFrame, incoming: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    if not incoming.empty:
        if incoming.date.lt(pd.Timestamp(start)).any() or incoming.date.gt(pd.Timestamp(end)).any():
            raise ReceiptError("仓单增量超出批准窗口")
        returned_days = pd.MultiIndex.from_frame(incoming[["symbol", "date"]].drop_duplicates())
        old_keys = pd.MultiIndex.from_frame(baseline[["symbol", "date"]])
        retained = baseline.loc[~old_keys.isin(returned_days), FIELDS.split(",")]
        rows = pd.concat([retained, incoming[FIELDS.split(",")]], ignore_index=True)
    else:
        rows = baseline[FIELDS.split(",")]
    return normalize_rows(rows.to_dict("records"))


def prepare_incremental(client, baseline_root: Path, candidate_root: Path, raw_root: Path,
                        now: dt.datetime, *, lookback_days: int = 14) -> dict:
    if now.tzinfo is None or not 1 <= lookback_days <= 90:
        raise ReceiptError("更新时间或回查范围无效")
    now = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    old_daily, old_manifest = load_snapshot(baseline_root)
    if set(old_daily.symbol) != set(PRODUCTS):
        raise ReceiptError("增量基线未覆盖六个品种")
    old_pointer = (baseline_root / "current.json").read_bytes()
    old_folder = baseline_root / "snapshots" / old_manifest["snapshot_id"]
    baseline = pd.read_csv(old_folder / "raw.csv", dtype={k:str for k in DIMENSIONS},
                           keep_default_na=False, na_values=[""])
    baseline = normalize_rows(baseline[FIELDS.split(",")].to_dict("records"))
    valid_baseline = old_daily.loc[old_daily.vol.notna()]
    if set(valid_baseline.symbol) != set(PRODUCTS):
        raise ReceiptError("增量基线缺少某品种的有效总量观测")
    latest = valid_baseline.groupby("symbol").date.max()
    end = now.date()
    start = max(pd.Timestamp(old_manifest["requested_start"]).date(), latest.min().date() - dt.timedelta(days=lookback_days - 1))
    if end < start:
        raise ReceiptError("更新时钟早于现有数据")
    # Calendar requests are limited to <1000 calendar days each, including gaps.
    calendar_rows = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=364), end)
        calendar_rows.extend(client.calendar(cursor, stop))
        cursor = stop + dt.timedelta(days=1)
    records = acquire_history(client, start, end, raw_root)
    rows = [row for record in records for row in record["rows"]]
    incoming = normalize_rows(rows) if rows else pd.DataFrame(columns=[*FIELDS.split(","), "date"])
    merged = merge_observations(baseline, incoming, start, end)
    daily = aggregate_daily(merged)
    # Use source calendar for the update window, retaining the documented packaged
    # calendar only for the older bootstrap history.
    from akshare.futures.cons import get_calendar
    prior_dates = old_manifest.get("trading_dates") or [pd.Timestamp(d).strftime("%Y-%m-%d") for d in get_calendar()]
    calendar = {d for d in prior_dates if old_manifest["requested_start"] <= d < start.isoformat()}
    calendar.update(pd.Timestamp(r["cal_date"]).strftime("%Y-%m-%d") for r in calendar_rows if r["is_open"] == 1)
    eligible = sorted(d for d in calendar if d <= end.isoformat())
    if not eligible:
        raise ReceiptError("无法确定最近交易日")
    expected = eligible[-1]
    latest_dates = {s: block.loc[block.vol.notna(), "date"].max().date().isoformat()
                    for s, block in daily.groupby("symbol")}
    pending = [s for s in PRODUCTS if latest_dates[s] < expected]
    changed = (merged[FIELDS.split(",")].to_csv(index=False) != baseline[FIELDS.split(",")].to_csv(index=False))
    candidate_root.mkdir(parents=True, exist_ok=False)
    if changed:
        evidence = dict(source=old_manifest["source"], endpoint=client.endpoint, api_name="fut_wsr",
            requested_start=old_manifest["requested_start"], requested_end=end.isoformat(), captured_at=now.isoformat(),
            previous_snapshot_id=old_manifest["snapshot_id"], update_window_start=start.isoformat(),
            query_windows=len(records), empty_windows=sum(not r["rows"] for r in records),
            trading_dates=sorted(calendar), calendar_source="AKShare bootstrap; Tushare DCE trade_cal update window",
            invalid_quantity_rows=int((merged.vol.lt(0) | merged.pre_vol.lt(0)).sum()),
            invalid_daily_totals=int(daily.status.eq("invalid_component").sum()),
            coverage={s:dict(first=b.date.min().date().isoformat(), last=b.date.max().date().isoformat(),
                            observed_days=len(b), units=sorted(b.unit.unique().tolist())) for s,b in daily.groupby("symbol")})
        manifest = write_snapshot(candidate_root, merged, daily, evidence)
    else:
        (candidate_root / "current.json").write_bytes(old_pointer)
        manifest = old_manifest
    if changed:
        load_snapshot(candidate_root)  # Full offline replay before any publication.
    if (baseline_root / "current.json").read_bytes() != old_pointer:
        raise ReceiptError("更新期间基线已改变，候选未发布")
    result = dict(schema_version="warehouse-update/1", status="WAITING_FOR_SOURCE" if pending else "UPDATED" if changed else "NO_CHANGE",
        checked_at=now.isoformat(), business_changed=changed, snapshot_id=manifest["snapshot_id"],
        expected_trade_date=expected, latest_dates=latest_dates, pending_products=pending,
        update_window_start=start.isoformat(), requested_end=end.isoformat(), source_windows=len(records),
        baseline_pointer_sha256=sha256(baseline_root / "current.json"), candidate_pointer_sha256=sha256(candidate_root / "current.json"))
    (candidate_root / "update_status.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (raw_root / "calendar.json").write_text(json.dumps(calendar_rows, ensure_ascii=False), encoding="utf-8")
    return result

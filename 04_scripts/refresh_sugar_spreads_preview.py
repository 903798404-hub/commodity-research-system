"""Refresh the isolated white sugar preview from bounded daily market reports."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.application.sugar_spreads import (
    DATA_PATH, DOMESTIC_CURVE_PATH, PREVIEW_FILES, PREVIEW_RELEASES, PREVIEW_ROOT,
    SugarQuote, contract_month, load_quotes, merge_quotes, resolve_preview_release, serialize_quotes,
)
from agri_research_agent.application.sugar_spread_sources import (
    LONDON, SHANGHAI, collect_reports, preview_dates,
)
from agri_research_agent.data_sources.tankan.domestic_spread import resolve_contract_season, window_contract_code
from agri_research_agent.domains.spreads.calculation import calculate_spread
from agri_research_agent.domains.spreads.models import SpreadCalculation, SpreadDefinition, SpreadLeg
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate


SR_SPREADS = {"SR 1-5", "SR 5-9", "SR 9-1"}


def domestic_spread_rows(quotes: list[SugarQuote], config: pd.DataFrame) -> list[dict]:
    if not quotes:
        return []
    day = quotes[0].trade_date
    if any(q.trade_date != day or q.price_type != "close" or q.provider != "郑商所" for q in quotes):
        raise ValueError("郑糖月差必须由同一交易日的郑商所收盘价计算")
    quotes_by_code = {quote.contract: quote for quote in quotes}
    rows = []
    for item in config.to_dict("records"):
        season = resolve_contract_season(day, **{key: int(item[key]) for key in
            ("window_start_month", "window_start_day", "window_end_month", "window_end_day")})
        if season is None:
            continue
        months = [int(item[f"leg{i}_month"]) for i in (1, 2)]
        names = [window_contract_code("SR", season.label, month, window_start_month=int(item["window_start_month"]))
                 for month in months]
        matches = [quotes_by_code.get(f"SR20{name[2:]}") for name in names]
        if not all(matches):
            continue
        legs = [SpreadLeg(ContractId(Exchange.CZCE, "SR", contract_month(q.contract).year, month))
                for q, month in zip(matches, months)]
        p1, p2 = (float(q.price) for q in matches)
        spread = SpreadDefinition(item["spread_name"], *legs, SpreadCalculation.DIFFERENCE)
        result = calculate_spread(spread, p1, p2)
        rows.append(dict(date=pd.Timestamp(day), spread_group="SR", spread_name=spread.name,
                         leg1_instrument="SR", leg1_month=months[0], leg1_price=p1, leg1_contract=names[0],
                         leg2_instrument="SR", leg2_month=months[1], leg2_price=p2, leg2_contract=names[1],
                         spread_value=result.value, season=season.label, calendar_offset=(day-season.start_date).days,
                         month_day=day.strftime("%m-%d"), status="success", error="",
                         updated_at=datetime.now(SHANGHAI).isoformat()))
    return rows


def extend_spread_database(prior: pa.Table, rows: list[dict]) -> pa.Table:
    if not rows:
        return prior
    old_sr = prior.filter(pc.is_in(prior["spread_name"], value_set=pa.array(sorted(SR_SPREADS))))
    original = {(pd.Timestamp(r["date"]), r["spread_name"], r["season"]): r
                for r in old_sr.select(["date", "spread_name", "season", "status", "leg1_price",
                                        "leg2_price", "spread_value"]).to_pylist()}
    if len(original) != len(old_sr):
        raise ValueError("原郑糖月差存在重复业务键")
    additions = []
    for row in rows:
        key = row["date"], row["spread_name"], row["season"]
        before = original.get(key)
        if before is not None:
            if before["status"] == "success" and any(before[k] != row[k] for k in
                                                    ("leg1_price", "leg2_price", "spread_value")):
                raise ValueError(f"郑糖月差历史收盘价存在冲突。{key}")
            continue
        original[key] = row
        additions.append(row)
    if not additions:
        return prior
    table = prior
    for key in ("leg1_contract", "leg2_contract"):
        if key not in table.column_names:
            table = table.append_column(key, pa.nulls(table.num_rows, pa.string()))
    frame = pd.DataFrame(additions)
    return pa.concat_tables([table, pa.Table.from_pandas(frame[table.column_names], schema=table.schema, preserve_index=False)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback", type=int, default=10, choices=range(1, 32))
    parser.add_argument("--through", type=date.fromisoformat, help="Limit both exchanges to this trading date")
    args = parser.parse_args()
    preview = ROOT / PREVIEW_ROOT
    releases = preview / PREVIEW_RELEASES
    if (not (ROOT / ".git").is_file() or not preview.is_dir()
            or preview.resolve() != preview or releases.resolve() != releases):
        parser.error("仅允许更新当前独立工作区已存在的白糖本地预览")
    sh, uk = datetime.now(SHANGHAI), datetime.now(LONDON)
    domestic_end = sh.date() - timedelta(days=sh.time() < time(15))
    foreign_end = uk.date() - timedelta(days=uk.time() < time(18, 30))
    if args.through:
        domestic_end, foreign_end = min(domestic_end, args.through), min(foreign_end, args.through)

    releases.mkdir(exist_ok=True)
    with FileLock(str(releases / ".refresh.lock"), timeout=0):
        previous = resolve_preview_release(ROOT, preview)
        if previous:
            paths = {name: previous / name for name in PREVIEW_FILES}
        else:
            paths = {DATA_PATH.name: preview / DATA_PATH, DOMESTIC_CURVE_PATH.name: preview / DOMESTIC_CURVE_PATH,
                     PREVIEW_FILES[2]: ROOT / "01_data" / PREVIEW_FILES[2]}
        old_foreign = load_quotes(paths[DATA_PATH.name], as_of=sh.date())
        old_domestic = load_quotes(paths[DOMESTIC_CURVE_PATH.name], as_of=sh.date())
        prior = pq.read_table(paths[PREVIEW_FILES[2]])
        config = pd.read_excel(ROOT / "02_configs/historical_spread_config.xlsx", sheet_name="spread_config")
        config = config.loc[config.spread_name.isin(SR_SPREADS)]
        if set(config.spread_name) != SR_SPREADS or len(config) != 3:
            raise ValueError("郑糖月差配置不完整或重复")
        domestic, domestic_receipts, domestic_raw = collect_reports(preview_dates(domestic_end, args.lookback),
                                                                  "domestic", old_domestic)
        foreign, foreign_receipts, foreign_raw = collect_reports(preview_dates(foreign_end, args.lookback),
                                                                  "foreign", old_foreign)
        current_domestic = merge_quotes(old_domestic + [q for batch in domestic for q in batch])
        current_foreign = merge_quotes(old_foreign + [q for batch in foreign for q in batch])
        spread_rows = [row for batch in domestic for row in domestic_spread_rows(batch, config)]
        table = extend_spread_database(prior, spread_rows)
        result = dict(status="no_change", domestic_latest=max(q.trade_date for q in current_domestic).isoformat(),
                      foreign_latest=max(q.trade_date for q in current_foreign if q.price_type=="settlement").isoformat(),
                      domestic_added=len(current_domestic)-len(old_domestic),
                      foreign_added=len(current_foreign)-len(old_foreign), monthly_added=len(table)-len(prior),
                      receipts=domestic_receipts + foreign_receipts)
        changed = any(result[key] for key in ("domestic_added", "foreign_added", "monthly_added"))
        result["failed_reports"] = sum(item["status"] == "failed" for item in result["receipts"])
        if not changed:
            result["status"] = "incomplete" if result["failed_reports"] else "no_change"
            print(json.dumps(result, ensure_ascii=False))
            return int(bool(result["failed_reports"]))

        result["status"] = "partial_update" if result["failed_reports"] else "updated"
        identifier = uuid4().hex[:24]

        def build(staging: Path) -> None:
            (staging / DATA_PATH.name).write_bytes(serialize_quotes(current_foreign))
            (staging / DOMESTIC_CURVE_PATH.name).write_bytes(serialize_quotes(current_domestic))
            pq.write_table(table, staging / PREVIEW_FILES[2])
            evidence = staging / "raw"
            evidence.mkdir()
            for name, raw in {**domestic_raw, **foreign_raw}.items():
                (evidence / name).write_bytes(raw)
            (staging / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            actual = pq.read_table(staging / PREVIEW_FILES[2])
            if not actual.slice(0, len(prior)).select(prior.column_names).equals(prior, check_metadata=True):
                raise ValueError("原价差历史被改变")
            for name, expected in [(DATA_PATH.name, current_foreign), (DOMESTIC_CURVE_PATH.name, current_domestic)]:
                if load_quotes(staging / name, as_of=sh.date()) != expected:
                    raise ValueError(f"候选行情往返核对失败。{name}")

        final, _ = seal_immutable_candidate(releases, identifier, build)
        files = {name: sha256((final / name).read_bytes()).hexdigest() for name in PREVIEW_FILES}
        atomic_write_json(releases / "current.json", dict(release_id=identifier, files=files))
        print(json.dumps({**result, "release": identifier}, ensure_ascii=False))
        return int(bool(result["failed_reports"]))


if __name__ == "__main__":
    raise SystemExit(main())

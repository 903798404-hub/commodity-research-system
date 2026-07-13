"""Local, normalized data layer for exchange member-position rankings.

The DCE/CZCE APIs may also return individual-contract tables.  This module
deliberately accepts only a row explicitly labelled with the requested variety
(P/Y/M/OI/RM), never rolls contracts up and never relabels a contract as a
main-contract position.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from io import BytesIO
from pathlib import Path
from typing import Callable

import akshare as ak
import pandas as pd
import requests
import yaml

VARIETIES = {"P": "DCE", "Y": "DCE", "M": "DCE", "OI": "CZCE", "RM": "CZCE"}
REQUIRED_COLUMNS = [
    "trade_date", "exchange", "variety", "seat_name_raw", "seat_name_normalized",
    "seat_group", "long_position", "long_change", "short_position", "short_change",
    "net_position", "net_change", "price_contract", "settlement_price", "data_status", "updated_at",
    "source_name", "source_method", "source_status", "source_date", "requested_date", "downloaded_at", "error_message",
]


def load_seat_config(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def normalize_seat(raw: object, config: dict[str, object]) -> str | None:
    # CZCE commonly appends a client-account marker such as （代客）.
    value = re.sub(r"[（(][^）)]*[）)]", "", str(raw or ""))
    value = re.sub(r"\s+", "", value).upper()
    for normalized, aliases in (config.get("aliases") or {}).items():
        candidates = [normalized, *(aliases or [])]
        normalized_candidates = {
            re.sub(r"\s+", "", re.sub(r"[（(][^）)]*[）)]", "", str(item))).upper()
            for item in candidates
        }
        if value in normalized_candidates:
            return str(normalized)
    return None


def seat_group(name: str, config: dict[str, object]) -> str:
    if name in set(config.get("foreign_seats") or []):
        return "foreign"
    if name in set(config.get("key_seats") or []):
        return "key"
    return "other"


def _call_with_retry(call: Callable[[], object], retries: int, timeout: int) -> object:
    error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(call)
                return future.result(timeout=timeout)
        except FutureTimeout as exc:
            error = TimeoutError(f"AKShare call exceeded {timeout}s")
        except Exception as exc:  # noqa: BLE001
            error = exc
        if attempt < retries:
            time.sleep(1 + attempt)
    raise error or RuntimeError("AKShare call failed")


def fetch_rank_tables(trade_date: dt.date, exchange: str, retries: int = 2, timeout: int = 30) -> dict[str, pd.DataFrame]:
    date_text = trade_date.strftime("%Y%m%d")
    call = (lambda: ak.futures_dce_position_rank(date=date_text, vars_list=["P", "Y", "M"])) if exchange == "DCE" else (lambda: ak.get_rank_table_czce(date=date_text))
    result = _call_with_retry(call, retries, timeout)
    return {str(key).upper(): value for key, value in (result or {}).items() if isinstance(value, pd.DataFrame)}


def _snapshot(raw_dir: Path, trade_date: dt.date, source: str, suffix: str, content: bytes, metadata: dict[str, object]) -> Path:
    folder = raw_dir / "foreign_seats" / "dce" / trade_date.strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    raw_file = folder / f"{source}.{suffix}"
    raw_file.write_bytes(content)
    raw_file.with_suffix(raw_file.suffix + ".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return raw_file


def _parse_dce_zip(content: bytes, trade_date: dt.date) -> dict[str, pd.DataFrame]:
    """Parse only explicitly named variety summary files; contract files are ignored."""
    tables: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(BytesIO(content)) as archive:
        for name in archive.namelist():
            stem = Path(name).stem.upper()
            variety = re.sub(r"[^A-Z]", "", stem)
            if variety not in {"P", "Y", "M"}:
                continue
            # A valid public product file is accepted only when its filename itself is P/Y/M.
            raw = pd.read_table(archive.open(name), header=None, sep="\t", encoding_errors="ignore")
            headers = raw.index[raw.iloc[:, 0].astype(str).str.contains("名次", na=False)].tolist()
            totals = raw.index[raw.iloc[:, 0].astype(str).str.contains("总计|合计", na=False)].tolist()
            if len(headers) < 3 or len(totals) < 3:
                continue
            parts = [raw.iloc[headers[i] + 1 : totals[i], :].reset_index(drop=True) for i in range(3)]
            table = pd.concat(parts, axis=1, ignore_index=True)
            if table.shape[1] < 12:
                continue
            table = table.iloc[:, :12]
            table.columns = ["_", "vol_party_name", "vol", "vol_chg", "_", "long_party_name", "long_open_interest", "long_open_interest_chg", "_", "short_party_name", "short_open_interest", "short_open_interest_chg"]
            tables[variety] = table[["long_party_name", "long_open_interest", "long_open_interest_chg", "short_party_name", "short_open_interest", "short_open_interest_chg"]]
    return tables


def fetch_dce_official_file(trade_date: dt.date, raw_dir: Path, timeout: int = 30) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    """Low-frequency public download fallback; no login or challenge bypass is used."""
    url = "http://www.dce.com.cn/dcereport/publicweb/dailystat/memberDealPosi/batchDownload"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; market-data/1.0)", "Referer": "http://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/rcjccpm/index.html", "Accept": "application/zip,application/octet-stream,*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    payload = {"tradeDate": trade_date.strftime("%Y%m%d"), "varietyId": "p", "contractId": "p2609", "tradeType": "1", "lang": "zh"}
    downloaded_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            response = session.post(url, json=payload, timeout=timeout)
        meta = {"url": url, "method": "POST JSON", "status_code": response.status_code, "requested_date": trade_date.isoformat(), "downloaded_at": downloaded_at, "content_type": response.headers.get("content-type", "")}
        suffix = "zip" if response.content[:2] == b"PK" else "html"
        _snapshot(raw_dir, trade_date, "dce_official", suffix, response.content, meta)
        if response.status_code != 200 or suffix != "zip":
            raise RuntimeError(f"official DCE download HTTP {response.status_code}; content_type={meta['content_type']}")
        return _parse_dce_zip(response.content, trade_date), {"source_name": "DCE official", "source_method": "public daily position ZIP", "source_status": "success", "source_date": trade_date.isoformat(), "requested_date": trade_date.isoformat(), "downloaded_at": downloaded_at, "error_message": ""}
    except Exception as exc:  # snapshot is retained whenever a response was received
        return {}, {"source_name": "DCE official", "source_method": "public daily position ZIP", "source_status": "failed", "source_date": "", "requested_date": trade_date.isoformat(), "downloaded_at": downloaded_at, "error_message": f"{type(exc).__name__}: {exc}"}


def fetch_dce_with_fallback(trade_date: dt.date, raw_dir: Path, retries: int = 2, timeout: int = 30) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    downloaded_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        tables = fetch_rank_tables(trade_date, "DCE", retries, timeout)
        if not tables:
            raise RuntimeError("AKShare returned no DCE tables (non-trading date or empty response)")
        serializable = {key: value.to_dict("records") for key, value in tables.items()}
        _snapshot(raw_dir, trade_date, "akshare", "json", json.dumps(serializable, ensure_ascii=False).encode("utf-8"), {"method": "ak.futures_dce_position_rank", "requested_date": trade_date.isoformat(), "downloaded_at": downloaded_at, "status": "success"})
        return tables, {"source_name": "AKShare", "source_method": "futures_dce_position_rank", "source_status": "success", "source_date": trade_date.isoformat(), "requested_date": trade_date.isoformat(), "downloaded_at": downloaded_at, "error_message": ""}
    except Exception as ak_exc:  # fallback is intentionally a single public request
        tables, provenance = fetch_dce_official_file(trade_date, raw_dir, timeout)
        if tables:
            return tables, provenance
        provenance["error_message"] = f"AKShare: {type(ak_exc).__name__}: {ak_exc}; fallback: {provenance['error_message']}"
        return {}, provenance


def _variety_table(tables: dict[str, pd.DataFrame], variety: str) -> pd.DataFrame:
    """Return only an explicitly exchange-published variety-level table."""
    wanted = variety.upper()
    for key, table in tables.items():
        key_letters = re.sub(r"[^A-Z]", "", key.upper())
        if key_letters == wanted:
            return table.copy()
    return pd.DataFrame()


def _side_rows(table: pd.DataFrame, *, side: str, config: dict[str, object]) -> dict[str, dict[str, object]]:
    name_col = f"{side}_party_name"
    position_col = f"{side}_open_interest"
    change_col = f"{side}_open_interest_chg"
    result: dict[str, dict[str, object]] = {}
    if table.empty or not {name_col, position_col, change_col}.issubset(table.columns):
        return result
    for _, row in table.iterrows():
        normalized = normalize_seat(row[name_col], config)
        if normalized:
            position = pd.to_numeric(str(row[position_col]).replace(",", ""), errors="coerce")
            change = pd.to_numeric(str(row[change_col]).replace(",", ""), errors="coerce")
            result[normalized] = {
                "raw": str(row[name_col]), "position": position, "change": change,
            }
    return result


def normalize_rankings(trade_date: dt.date, exchange: str, variety: str, tables: dict[str, pd.DataFrame], config: dict[str, object], updated_at: str, provenance: dict[str, object] | None = None) -> list[dict[str, object]]:
    table = _variety_table(tables, variety)
    long_rows, short_rows = _side_rows(table, side="long", config=config), _side_rows(table, side="short", config=config)
    tracked = set(config.get("foreign_seats") or []) | set(config.get("key_seats") or [])
    rows: list[dict[str, object]] = []
    for name in sorted(tracked):
        long_item, short_item = long_rows.get(name), short_rows.get(name)
        listed = long_item is not None or short_item is not None
        long_pos = long_item["position"] if long_item else pd.NA
        short_pos = short_item["position"] if short_item else pd.NA
        long_change = long_item["change"] if long_item else pd.NA
        short_change = short_item["change"] if short_item else pd.NA
        # A side not ranked is unknown, rather than zero.  Net values require both sides.
        net = long_pos - short_pos if pd.notna(long_pos) and pd.notna(short_pos) else pd.NA
        net_change = long_change - short_change if pd.notna(long_change) and pd.notna(short_change) else pd.NA
        rows.append({"trade_date": trade_date.isoformat(), "exchange": exchange, "variety": variety,
            "seat_name_raw": (long_item or short_item or {}).get("raw", pd.NA), "seat_name_normalized": name,
            "seat_group": seat_group(name, config), "long_position": long_pos, "long_change": long_change,
            "short_position": short_pos, "short_change": short_change, "net_position": net, "net_change": net_change,
            "price_contract": f"{variety}0", "settlement_price": pd.NA,
            "data_status": "listed" if listed else "not_ranked", "updated_at": updated_at, **(provenance or {})})
    return rows


def source_error_rows(trade_date: dt.date, exchange: str, config: dict[str, object], updated_at: str, provenance: dict[str, object] | None = None) -> list[dict[str, object]]:
    """Persist fetch failures explicitly; they must not appear as zero or unranked."""
    rows: list[dict[str, object]] = []
    tracked = set(config.get("foreign_seats") or []) | set(config.get("key_seats") or [])
    for variety, mapped_exchange in VARIETIES.items():
        if mapped_exchange != exchange:
            continue
        for name in sorted(tracked):
            rows.append({"trade_date": trade_date.isoformat(), "exchange": exchange, "variety": variety,
                "seat_name_raw": pd.NA, "seat_name_normalized": name, "seat_group": seat_group(name, config),
                "long_position": pd.NA, "long_change": pd.NA, "short_position": pd.NA, "short_change": pd.NA,
                "net_position": pd.NA, "net_change": pd.NA, "price_contract": f"{variety}0",
                "settlement_price": pd.NA, "data_status": "source_error", "updated_at": updated_at, **(provenance or {})})
    return rows


def fetch_main_prices(start: dt.date, end: dt.date, retries: int = 2, timeout: int = 30) -> pd.DataFrame:
    frames = []
    for variety in VARIETIES:
        data = _call_with_retry(lambda v=variety: ak.futures_main_sina(symbol=f"{v}0", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")), retries, timeout)
        data = data.copy()
        data["trade_date"] = pd.to_datetime(data["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        data["variety"] = variety
        data["price_contract"] = f"{variety}0"
        data["settlement_price"] = pd.to_numeric(data.get("动态结算价"), errors="coerce")
        frames.append(data[["trade_date", "variety", "price_contract", "settlement_price"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["trade_date", "variety", "price_contract", "settlement_price"])


def write_parquet(rows: list[dict[str, object]], prices: pd.DataFrame, path: Path) -> pd.DataFrame:
    new = pd.DataFrame(rows, columns=REQUIRED_COLUMNS)
    if not prices.empty and not new.empty:
        new = new.drop(columns=["price_contract", "settlement_price"]).merge(prices, on=["trade_date", "variety"], how="left")
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=REQUIRED_COLUMNS)
    combined = pd.concat([existing, new], ignore_index=True)
    combined = combined.drop_duplicates(["trade_date", "exchange", "variety", "seat_name_normalized"], keep="last")
    combined = combined.sort_values(["trade_date", "exchange", "variety", "seat_name_normalized"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path, index=False)
    return combined


def write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

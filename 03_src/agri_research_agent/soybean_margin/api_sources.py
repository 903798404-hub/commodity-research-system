"""Bounded Databento Historical and AkShare/CFETS adapters. No live subscriptions."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

import requests

from .api_inputs import CUTOVER, SCHEMA, validate_snapshot
from .model import contracts, number

SHANGHAI = ZoneInfo("Asia/Shanghai")
HIST_URL = "https://hist.databento.com/v0"
MAX_RESPONSE = 256 * 1024
MAX_AGE_SECONDS = 300
MONTH_CODES = {1: "F", 3: "H", 5: "K", 7: "N", 8: "Q", 9: "U", 11: "X"}


class SourceError(ValueError):
    """Only fixed, non-secret error codes may cross the provider boundary."""


def credential(path: Path | None = None):
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if path is not None:
        path = Path(path)
        if path.stat().st_size > 4096:
            raise SourceError("credential_invalid")
        lines = path.read_text(encoding="utf-8").splitlines()
        matches = [line.split("=", 1)[1].strip() for line in lines
                   if line.startswith("DATABENTO_API_KEY=")]
        if len(matches) != 1:
            raise SourceError("credential_invalid")
        key = matches[0]
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", key):
        raise SourceError("credential_missing_or_invalid")
    return key


def raw_symbol(code: str):
    if not re.fullmatch(r"\d{4}", code) or int(code[2:]) not in MONTH_CODES:
        raise SourceError("cbot_contract_invalid")
    return "ZS" + MONTH_CODES[int(code[2:])] + code[1]


def window(day: date):
    end = datetime.combine(day, time(9), SHANGHAI).astimezone(timezone.utc)
    return end - timedelta(minutes=1), end


def _payload(response):
    response.raise_for_status()
    chunks, size = [], 0
    for block in response.iter_content(16384):
        size += len(block)
        if size > MAX_RESPONSE:
            raise SourceError("response_too_large")
        chunks.append(block)
    return b"".join(chunks).decode("utf-8")


def historical_cbot(day: date, key: str, *, session=requests):
    codes = sorted({contracts(day, month)[0] for month in range(1, 13)})
    symbols = {raw_symbol(code): code for code in codes}
    if len(symbols) != len(codes):
        raise SourceError("cbot_symbol_collision")
    prices = dict.fromkeys(codes)
    start, end = window(day)
    metadata = dict(provider="Databento", dataset="GLBX.MDP3", schema="ohlcv-1m",
                    stype_in="raw_symbol", symbols=symbols, window_start=start.isoformat(),
                    window_end=end.isoformat(), price_field="close", status="missing")
    try:
        with session.post(HIST_URL + "/timeseries.get_range", auth=(key, ""),
                          data=dict(dataset="GLBX.MDP3", schema="ohlcv-1m",
                                    symbols=",".join(symbols), stype_in="raw_symbol",
                                    start=start.isoformat(), end=end.isoformat(), encoding="json",
                                    pretty_px="true", pretty_ts="true", map_symbols="true"),
                          timeout=(10, 20), stream=True) as response:
            if response.status_code == 422:
                body = json.loads(_payload_error(response))
                detail = body.get("detail", {})
                if isinstance(detail, dict) and detail.get("case") == "dataset_unavailable_range":
                    metadata["status"] = "historical_not_yet_available"
                    return prices, metadata
                raise SourceError("historical_request_rejected")
            raw = _payload(response)
        seen = set()
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            symbol = record.get("symbol")
            stamp = datetime.fromisoformat(record["hd"]["ts_event"].replace("Z", "+00:00"))
            if symbol not in symbols or stamp != start or symbol in seen:
                raise SourceError("cbot_record_identity_invalid")
            price = number(record.get("close"))
            if price is None or price <= 0:
                raise SourceError("cbot_price_invalid")
            seen.add(symbol)
            prices[symbols[symbol]] = price
        metadata["status"] = "available" if seen else "no_trades_in_target_minute"
        return prices, metadata
    except SourceError:
        raise
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise SourceError("historical_transport_or_schema_error") from None


def _payload_error(response):
    # Read a bounded rejection body without raise_for_status or exposing request auth.
    chunks, size = [], 0
    for block in response.iter_content(4096):
        size += len(block)
        if size > 16384:
            raise SourceError("historical_error_body_invalid")
        chunks.append(block)
    return b"".join(chunks).decode("utf-8")


def fresh(stamp: datetime, now: datetime):
    if stamp.utcoffset() is None or now.utcoffset() is None:
        raise SourceError("quote_timezone_missing")
    age = (now - stamp).total_seconds()
    if stamp.astimezone(SHANGHAI).date() != now.astimezone(SHANGHAI).date() or not 0 <= age <= MAX_AGE_SECONDS:
        raise SourceError("quote_stale_or_future")


def fx_curve(now: datetime, *, session=requests):
    from akshare.fx.fx_quote import FX_SPOT_URL, FX_SWAP_URL, SHORT_HEADERS
    responses = []
    for url in (FX_SPOT_URL, FX_SWAP_URL):
        with session.post(url.replace("http://", "https://"), headers=SHORT_HEADERS,
                          timeout=(10, 15), stream=True) as response:
            body = json.loads(_payload(response))
        stamp = datetime.fromisoformat(body["data"]["showDateCN"]).replace(tzinfo=SHANGHAI)
        fresh(stamp, now)
        responses.append((body, stamp))
    spot, swaps = responses[0][0], responses[1][0]
    matches = [r for r in spot["records"] if r.get("ccyPair") == "USD/CNY"]
    if len(matches) != 1:
        raise SourceError("fx_spot_pair_missing_or_duplicate")
    bid, ask = (number(matches[0].get(k)) for k in ("bidPrc", "askPrc"))
    if bid is None or ask is None or not 0 < bid <= ask:
        raise SourceError("fx_spot_bid_ask_invalid")
    curve = {"0": (bid + ask) / 2}
    quotes = {}
    rows = [r for r in swaps["records"] if r.get("ccyPair") == "USD/CNY"]
    if len(rows) != 1:
        raise SourceError("fx_swap_pair_missing_or_duplicate")
    for month, label in ((1, "1M"), (3, "3M"), (6, "6M"), (9, "9M"), (12, "1Y")):
        text = rows[0].get("label_" + label)
        if text in (None, "", "---", "--"):
            curve[str(month)] = None
            continue
        parts = text.split("/")
        if len(parts) != 2:
            raise SourceError("fx_swap_points_invalid")
        low, high = map(number, parts)
        if low is None or high is None or low > high:
            raise SourceError("fx_swap_points_invalid")
        full = curve["0"] + (low + high) / 2 / 10000
        if full <= 0:
            raise SourceError("fx_forward_invalid")
        curve[str(month)] = full
        quotes[str(month)] = text
    return curve, dict(provider="CFETS via AkShare endpoints", currency="USD/CNY",
                       rate_kind="mid", spot_bid=bid, spot_ask=ask, swap_points=quotes,
                       points_divisor=10000, published_at=[stamp.isoformat() for _, stamp in responses])


def domestic_worker(symbols: list[str]):
    """Run AkShare in a disposable process; preserve Sina's omitted quote date."""
    import akshare as ak
    from unittest.mock import patch

    result, evidence, errors = dict.fromkeys(symbols), {}, {}
    original = requests.get
    for symbol in symbols:
        raw_fields = []

        def get(url, **kwargs):
            kwargs.setdefault("timeout", (10, 10))
            response = original(url, **kwargs)
            response.raise_for_status()
            if response.url.startswith("https://hq.sinajs.cn/"):
                if len(response.content) > 16384:
                    raise SourceError("domestic_response_too_large")
                match = re.fullmatch(r'\s*var hq_str_nf_' + re.escape(symbol) + r'="([^"\r\n]*)";\s*', response.text)
                if not match or not match[1]:
                    raise SourceError("contract_not_available")
                fields = match[1].split(",")
                if len(fields) < 28:
                    raise SourceError("domestic_schema_invalid")
                if not fields[0].endswith(symbol[-4:]):
                    raise SourceError("domestic_contract_identity_invalid")
                raw_fields.extend(fields)
                # AkShare 1.18.64 expects the original 28 columns; Sina now appends depth.
                adapted = copy.copy(response)
                adapted._content = ('var hq_str_nf_' + symbol + '="' + ",".join(fields[:28]) + '";').encode(response.encoding or "utf-8")
                return adapted
            raise SourceError("unexpected_domestic_endpoint")

        try:
            with patch("requests.get", get):
                frame = ak.futures_zh_spot(symbol=symbol, market="CF", adjust="0")
            if len(frame) != 1 or not raw_fields:
                raise SourceError("domestic_quote_missing")
            stamp = datetime.combine(date.fromisoformat(raw_fields[17]),
                                     datetime.strptime(raw_fields[1].zfill(6), "%H%M%S").time(), SHANGHAI)
            fresh(stamp, datetime.now(SHANGHAI))
            price = number(frame.iloc[0]["current_price"])
            if price is None or price <= 0 or price != number(raw_fields[8]):
                raise SourceError("domestic_price_invalid")
            result[symbol] = price
            evidence[symbol] = dict(quoted_at=stamp.isoformat(), price_field="current_price", raw_contract=raw_fields[0])
        except Exception as exc:
            errors[symbol] = str(exc) if isinstance(exc, SourceError) else "domestic_transport_or_schema_error"
    return result, dict(provider="AkShare/Sina", akshare_version=ak.__version__, quotes=evidence), errors


def trading_calendar():
    import akshare as ak
    from unittest.mock import patch
    original = requests.get

    def get(url, **kwargs):
        kwargs.setdefault("timeout", (10, 15))
        return original(url, **kwargs)

    with patch("requests.get", get):
        values = ak.tool_trade_date_hist_sina()["trade_date"]
    return [str(item) for item in values]


def _worker(argument, timeout):
    try:
        process = subprocess.run([sys.executable, "-m", __name__, argument],
                                 capture_output=True, timeout=timeout, encoding="utf-8",
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                      "PYTHONPATH": str(Path(__file__).resolve().parents[2]) + os.pathsep + os.getenv("PYTHONPATH", "")}, check=True)
        if len(process.stdout.encode("utf-8")) > MAX_RESPONSE:
            raise SourceError("worker_response_too_large")
        return json.loads(process.stdout)
    except (subprocess.SubprocessError, ValueError):
        raise SourceError("akshare_worker_failed") from None


def domestic(symbols):
    return _worker(json.dumps(symbols), 100)


def capture(key: str, *, now: datetime | None = None):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    day = now.date()
    if day < CUTOVER or day.weekday() >= 5 or not time(9, 35) <= now.time() <= time(15):
        raise SourceError("capture_outside_same_day_0935_1500_window")
    # A quote response is never used as a trading-calendar substitute.
    dates = {date.fromisoformat(item) for item in _worker("calendar", 35)}
    if not dates or day > max(dates):
        raise SourceError("trading_calendar_out_of_range")
    if day not in dates:
        raise SourceError("domestic_market_holiday")
    cbots = sorted({contracts(day, m)[0] for m in range(1, 13)})
    dce = sorted({p + contracts(day, m)[1] for p in ("M", "Y") for m in range(1, 13)})
    snapshot = dict(schema_version=SCHEMA, business_date=day.isoformat(), captured_at=now.isoformat(),
                    cbot_unit="US_cents/bushel", domestic_unit="CNY/tonne",
                    fx_currency="USD/CNY", fx_unit="CNY_per_USD", cbot=dict.fromkeys(cbots),
                    domestic=dict.fromkeys(dce), fx_curve={}, errors={},
                    sources={"cbot": {}, "domestic": {}, "fx": {}},
                    calendar=dict(provider="AkShare/Sina", covered_through=max(dates).isoformat()))
    try:
        snapshot["cbot"], snapshot["sources"]["cbot"] = historical_cbot(day, key)
    except SourceError as exc:
        snapshot["errors"]["cbot"] = str(exc)
    try:
        snapshot["fx_curve"], snapshot["sources"]["fx"] = fx_curve(datetime.now(SHANGHAI))
    except Exception as exc:
        snapshot["errors"]["fx"] = str(exc) if isinstance(exc, SourceError) else "fx_transport_or_schema_error"
    try:
        snapshot["domestic"], snapshot["sources"]["domestic"], errors = domestic(dce)
        snapshot["errors"].update(errors)
    except SourceError as exc:
        snapshot["errors"]["domestic"] = str(exc)
    snapshot["captured_at"] = datetime.now(SHANGHAI).isoformat()
    validate_snapshot(snapshot)
    return snapshot


def backfill(snapshot: dict, key: str):
    day = validate_snapshot(snapshot)
    prices, metadata = historical_cbot(day, key)
    if metadata["status"] == "historical_not_yet_available":
        raise SourceError("historical_not_yet_available")
    result = copy.deepcopy(snapshot)
    manual = result["sources"]["cbot"].get("manual")
    metadata["fetched_at"] = datetime.now(SHANGHAI).isoformat()
    metadata["prices"] = dict(prices)
    result["cbot"] = prices
    if manual:
        for code, value in manual["prices"].items():
            if code in result["cbot"]:
                result["cbot"][code] = float(value)
        result["sources"]["cbot"] = dict(provider="user_upload / Databento Historical",
                status="manual_with_historical", manual=manual, historical=metadata)
    else:
        result["sources"]["cbot"] = metadata
    result["errors"].pop("cbot", None)
    return result


def manual_cbot(snapshot: dict, prices: dict, evidence: dict, *, quoted_at: str | None = None):
    """Apply user-confirmed full contract codes; retain provided data ahead of backfills."""
    day = validate_snapshot(snapshot)
    if not isinstance(prices, dict) or not 1 <= len(prices) <= 8:
        raise SourceError("manual_cbot_contracts_invalid")
    for code, price in prices.items():
        if (not isinstance(code, str) or not re.fullmatch(r"\d{4}", code) or int(code[2:]) not in MONTH_CODES
                or number(price) is None or number(price) <= 0):
            raise SourceError("manual_cbot_price_or_contract_invalid")
        year = 2000 + int(code[:2])
        if not day.year <= year <= day.year + 2:
            raise SourceError("manual_cbot_year_invalid")
    if not isinstance(evidence, dict) or not re.fullmatch(r"[a-f0-9]{64}", evidence.get("sha256", "")):
        raise SourceError("manual_cbot_evidence_invalid")
    if evidence.get("filename") not in {evidence["sha256"] + ".png", evidence["sha256"] + ".jpg"}:
        raise SourceError("manual_cbot_evidence_invalid")
    if quoted_at is not None:
        stamp = datetime.fromisoformat(quoted_at)
        if stamp.utcoffset() is None or stamp.astimezone(SHANGHAI).date() != day:
            raise SourceError("manual_cbot_quote_date_invalid")
    result = copy.deepcopy(snapshot)
    # A new user upload fully replaces prior manual quotes for this day.
    # It does not inherit absent screenshot contracts or former browser observations.
    historical = result["sources"]["cbot"]
    if historical.get("provider") == "Databento":
        historical = dict(historical, prices=dict(result["cbot"]))
    else:
        historical = historical.get("historical", {})
        result["cbot"] = {code: historical.get("prices", {}).get(code) for code in result["cbot"]}
    for code, price in prices.items():
        if code in result["cbot"]:
            result["cbot"][code] = float(price)
    result["sources"]["cbot"] = dict(provider="user_upload / Databento Historical", status="manual_quotes",
        manual=dict(prices=prices, evidence=evidence, quoted_at=quoted_at,
                    received_at=datetime.now(SHANGHAI).isoformat(), price_field="latest",
                    unit="US_cents/bushel", unused_contracts=sorted(set(prices) - set(result["cbot"]))),
        historical=historical)
    result["errors"].pop("cbot", None)
    validate_snapshot(result)
    return result


if __name__ == "__main__":
    if sys.argv[1] == "calendar":
        print(json.dumps(trading_calendar()))
        raise SystemExit(0)
    symbols = json.loads(sys.argv[1])
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 8 or any(not re.fullmatch(r"(?:M|Y|P|RM|OI)\d{4}", s) for s in symbols):
        raise SystemExit(2)
    print(json.dumps(domestic_worker(symbols), ensure_ascii=False, allow_nan=False))

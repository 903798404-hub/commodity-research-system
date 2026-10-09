"""Replay official FX history and independent latest-publication references."""
from __future__ import annotations

import base64
from datetime import date, datetime
import gzip
import hashlib
from html.parser import HTMLParser
import io
from pathlib import Path
import re

from .foreign_fx import BY_CODE, ECB_HISTORY_URL, FxDataError, bcb_url, parse_bcb, parse_ecb, validate_snapshot
from .foreign_fx_update import MAX_RESPONSE_BYTES, collect_snapshot, fetch_official

BCB_LATEST_URL = "https://ptax.bcb.gov.br/ptax_internet/consultarUltimaCotacaoDolar.do"
ECB_LATEST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
EVIDENCE_SCHEMA = "foreign-fx-source-evidence/1"
START = date(2021, 1, 1)


def exact(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise FxDataError(f"{label} fields differ")
    return value


def canonical(value):
    import json
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def bcb_latest(raw):
    # This official legacy page declares ISO-8859-1, unlike SGS JSON.
    parser = Text()
    parser.feed(raw.decode("iso-8859-1", errors="strict"))
    text = " ".join(parser.parts)
    dates = re.findall(r"Cotação de fechamento do dólar no dia\s+(\d{2}/\d{2}/\d{4})", text)
    if len(dates) != 1:
        raise FxDataError("BCB latest closing publication date is missing or ambiguous")
    quotes = re.findall(r"(\d{2}/\d{2}/\d{4})\s+([0-9]+,[0-9]+)\s+([0-9]+,[0-9]+)", text)
    if len(quotes) != 1 or quotes[0][0] != dates[0]:
        raise FxDataError("BCB latest closing sale quote missing or ambiguous")
    return datetime.strptime(dates[0], "%d/%m/%Y").date().isoformat(), float(quotes[0][2].replace(",", "."))


def _raw_record(url, raw):
    if not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise FxDataError("FX raw response exceeds bound")
    return {"url": url, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
            "gzip_base64": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")}


def _decode(record, url):
    exact(record, ("url", "sha256", "size_bytes", "gzip_base64"), "FX raw record")
    if record["url"] != url or type(record["size_bytes"]) is not int or not 0 < record["size_bytes"] <= MAX_RESPONSE_BYTES:
        raise FxDataError("FX raw URL or byte count differs")
    encoded = record["gzip_base64"]
    if type(encoded) is not str or len(encoded) > 2 * MAX_RESPONSE_BYTES:
        raise FxDataError("FX encoded response exceeds bound")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
    except (ValueError, OSError, EOFError) as exc:
        raise FxDataError("FX raw archive invalid") from exc
    if len(raw) != record["size_bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise FxDataError("FX raw identity differs")
    return raw


def collect(end: date, raw_directory: Path, *, fetcher=fetch_official):
    snapshot = collect_snapshot(START, end, raw_directory=raw_directory, fetcher=fetcher)
    references = {"bcb_latest": (BCB_LATEST_URL, fetcher(BCB_LATEST_URL)),
                  "ecb_latest": (ECB_LATEST_URL, fetcher(ECB_LATEST_URL))}
    for key, (_url, raw) in references.items():
        (raw_directory / (key + ".raw")).write_bytes(raw)
    evidence = {"schema_version": EVIDENCE_SCHEMA, "request_start": START.isoformat(),
                "request_end": end.isoformat(), "raw": {
                    "bcb_history": _raw_record(bcb_url(START, end), (raw_directory / "BCB_SGS1.json").read_bytes()),
                    "ecb_history": _raw_record(ECB_HISTORY_URL, (raw_directory / "ECB_CROSS.xml").read_bytes()),
                    **{key: _raw_record(url, raw) for key, (url, raw) in references.items()}}}
    observations(snapshot, None, evidence)
    return snapshot, evidence


def replay(snapshot, evidence):
    exact(snapshot, ("schema_version", "generated_at", "sources", "observations"), "FX snapshot")
    frame = validate_snapshot(snapshot)
    if set(frame.currency) != set(BY_CODE):
        raise FxDataError("FX currency coverage incomplete")
    exact(evidence, ("schema_version", "request_start", "request_end", "raw"), "FX evidence")
    if evidence["schema_version"] != EVIDENCE_SCHEMA or evidence["request_start"] != START.isoformat():
        raise FxDataError("FX replay scope differs")
    end = date.fromisoformat(evidence["request_end"])
    if end.isoformat() != evidence["request_end"] or end < START:
        raise FxDataError("FX replay date invalid")
    records = exact(evidence["raw"], ("bcb_history", "ecb_history", "bcb_latest", "ecb_latest"), "FX raw set")
    raws = {key: _decode(records[key], url) for key, url in {
        "bcb_history": bcb_url(START, end), "ecb_history": ECB_HISTORY_URL,
        "bcb_latest": BCB_LATEST_URL, "ecb_latest": ECB_LATEST_URL}.items()}
    rows = sorted(parse_bcb(raws["bcb_history"], START, end) + parse_ecb(raws["ecb_history"], START, end),
                  key=lambda row: (row["currency"], row["date"]))
    if rows != snapshot["observations"]:
        raise FxDataError("FX observations differ from official raw replay")
    sources = {source["provider"]: source for source in snapshot["sources"]}
    if set(sources) != {"BCB_SGS1", "ECB_CROSS"}:
        raise FxDataError("FX provider set differs")
    for provider, key in (("BCB_SGS1", "bcb_history"), ("ECB_CROSS", "ecb_history")):
        source = exact(sources[provider], ("provider", "url", "raw_sha256", "retrieved_at", "method"), "FX source")
        if source["url"] != records[key]["url"] or source["raw_sha256"] != records[key]["sha256"]:
            raise FxDataError("FX snapshot source identity differs")
        if source["method"] != ("PTAX卖出参考汇率" if provider == "BCB_SGS1" else "同日EUR/本币除以EUR/USD"):
            raise FxDataError("FX conversion method differs")
        if datetime.fromisoformat(source["retrieved_at"]).tzinfo is None:
            raise FxDataError("FX source capture time lacks timezone")
    for day, daily in frame.loc[frame.provider == "ECB_CROSS"].groupby("date"):
        if set(daily.currency) != set(BY_CODE) - {"BRL"}:
            raise FxDataError(f"ECB date has partial coverage: {day}")
    expected_bcb, bcb_sale = bcb_latest(raws["bcb_latest"])
    ecb = parse_ecb(raws["ecb_latest"], START, end)
    expected_ecb = max(row["date"] for row in ecb)
    if len({row["date"] for row in ecb}) != 1 or {row["currency"] for row in ecb} != set(BY_CODE) - {"BRL"}:
        raise FxDataError("ECB latest reference has partial coverage")
    latest = {code: max(row["date"] for row in rows if row["currency"] == code) for code in BY_CODE}
    if latest["BRL"] != expected_bcb or any(latest[code] != expected_ecb for code in BY_CODE if code != "BRL"):
        raise FxDataError("History has not reached the official latest publication; retry required")
    index = {(row["currency"], row["date"]): row for row in rows}
    if index[("BRL", expected_bcb)]["local_per_usd"] != bcb_sale:
        raise FxDataError("BCB latest closing sale quote differs from history")
    if any(index[(row["currency"], row["date"])] != row for row in ecb):
        raise FxDataError("ECB latest fixing differs from history")
    return rows, latest


def observations(snapshot, baseline, evidence):
    rows, latest = replay(snapshot, evidence)
    before = {} if baseline is None else {(r["currency"], r["date"]): r for r in baseline["observations"]}
    if baseline is not None:
        validate_snapshot(baseline)
    after = {(r["currency"], r["date"]): r for r in rows}
    if set(before) - set(after):
        raise FxDataError("FX candidate removes published historical observations")
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    previous_sources = {} if baseline is None else {item["provider"]: item["raw_sha256"] for item in baseline["sources"]}
    current_sources = {item["provider"]: item["raw_sha256"] for item in snapshot["sources"]}
    return {"record_count": len(rows), "latest_dates": latest, "added": len(set(after) - set(before)),
            "revised": len(changed), "business_changed": baseline is None or bool(set(after) - set(before) or changed),
            "revision_keys": ["/".join(key) for key in changed],
            "revisions": [{"currency": key[0], "date": key[1], "previous": before[key]["local_per_usd"],
                "current": after[key]["local_per_usd"], "previous_raw_sha256": previous_sources[before[key]["provider"]],
                "current_raw_sha256": current_sources[after[key]["provider"]]} for key in changed]}

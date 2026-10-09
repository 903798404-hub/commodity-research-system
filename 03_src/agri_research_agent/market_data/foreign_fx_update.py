"""Source-replayed FX update in a caller-owned workspace; no scheduling or delivery."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen
from uuid import uuid4

from agri_research_agent.shared.atomic_storage import atomic_write_bytes, atomic_write_json
from .foreign_fx import (
    BY_CODE, ECB_HISTORY_URL, SCHEMA_VERSION, FxDataError, bcb_url,
    load_snapshot, parse_bcb, parse_ecb, validate_snapshot,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def fetch_official(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "AgriculturalResearch/1.0"}), timeout=45) as response:
        if response.status != 200:
            raise FxDataError(f"Official source HTTP {response.status}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FxDataError("Official response exceeds size limit")
    return raw


def collect_snapshot(start: date, end: date, *, raw_directory: Path | None = None,
                     fetcher: Callable[[str], bytes] = fetch_official) -> dict:
    if not start <= end <= date.today():
        raise FxDataError("Request dates are invalid")
    sources, rows = [], []
    for provider, url, parser, extension in (
        ("BCB_SGS1", bcb_url(start, end), parse_bcb, "json"),
        ("ECB_CROSS", ECB_HISTORY_URL, parse_ecb, "xml"),
    ):
        raw = fetcher(url)
        # Preserve the exact received bytes, including an invalid response for diagnosis.
        if raw_directory is not None:
            raw_directory.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(raw_directory / f"{provider}.{extension}", raw)
        rows.extend(parser(raw, start, end))
        sources.append(dict(provider=provider, url=url, raw_sha256=hashlib.sha256(raw).hexdigest(),
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                            method="PTAX卖出参考汇率" if provider == "BCB_SGS1" else "同日EUR/本币除以EUR/USD"))
    payload = dict(schema_version=SCHEMA_VERSION, generated_at=datetime.now(timezone.utc).isoformat(),
                   sources=sources, observations=sorted(rows, key=lambda row: (row["currency"], row["date"])))
    frame = validate_snapshot(payload)
    if set(frame["currency"]) != set(BY_CODE):
        raise FxDataError("Official source coverage is incomplete")
    # Reject partially published ECB daily rows instead of mixing seven latest dates.
    for day, daily in frame.loc[frame["provider"] == "ECB_CROSS"].groupby("date"):
        if set(daily["currency"]) != set(BY_CODE) - {"BRL"}:
            raise FxDataError(f"ECB reference date has incomplete currency coverage: {day}")
    return payload


def _index(payload: dict) -> dict:
    return {(row["currency"], row["date"]): row for row in payload["observations"]}


def run_update(workspace: Path, start: date, end: date, *,
               fetcher: Callable[[str], bytes] = fetch_official) -> dict:
    """Keep raw, candidate, revision and recovery evidence beside this workspace's stable."""
    workspace.mkdir(parents=True, exist_ok=True)
    lock = workspace / "update.lock"
    # Contending callers do not delete somebody else's lock or alter their status.
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(datetime.now(timezone.utc).isoformat())
    stable = workspace / "daily.json"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex
    run_dir = workspace / "runs" / run_id
    previous_bytes = None
    switched = False
    status = dict(schema_version="foreign-fx-update/1", run_id=run_id,
                  stable_sha256=None, latest_by_currency={}, published_at=None)
    try:
        run_dir.mkdir(parents=True)
        previous = None
        if stable.exists():
            previous_bytes = stable.read_bytes()
            previous, _ = load_snapshot(stable)
            status.update(stable_sha256=hashlib.sha256(previous_bytes).hexdigest(),
                          published_at=previous["generated_at"],
                          latest_by_currency={code: max(row["date"] for row in previous["observations"]
                                                   if row["currency"] == code)
                                              for code in BY_CODE if any(row["currency"] == code for row in previous["observations"])})
        candidate = collect_snapshot(start, end, raw_directory=run_dir / "raw", fetcher=fetcher)
        atomic_write_json(run_dir / "candidate.json", candidate)
        before, after = _index(previous) if previous else {}, _index(candidate)
        removed = set(before) - set(after)
        if removed:
            raise FxDataError(f"Candidate drops {len(removed)} previously published observations")
        revisions = [dict(currency=key[0], date=key[1], previous=before[key]["local_per_usd"],
                          current=after[key]["local_per_usd"])
                     for key in sorted(set(before) & set(after)) if before[key] != after[key]]
        added = len(set(after) - set(before))
        atomic_write_json(run_dir / "validation.json", dict(result="PASS", added=added, revisions=revisions))
        if added or revisions:
            if previous_bytes is not None:
                # The byte-identical recovery copy must exist before publishing the candidate.
                atomic_write_bytes(run_dir / "previous.json", previous_bytes)
            encoded = (json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            atomic_write_bytes(stable, encoded)
            switched = True
            status.update(result="UPDATED", stable_sha256=hashlib.sha256(encoded).hexdigest(),
                          published_at=candidate["generated_at"],
                          latest_by_currency={code: max(row["date"] for row in candidate["observations"]
                                                       if row["currency"] == code) for code in BY_CODE})
        else:
            status["result"] = "NO_CHANGE"
        status.update(checked_at=datetime.now(timezone.utc).isoformat(), added=added, revised=len(revisions))
        atomic_write_json(run_dir / "status.json", status)
        atomic_write_json(workspace / "status.json", status)
        return status
    except Exception as exc:
        # A status write failure after publication must not claim the old file is still current.
        status.update(result="FAILED_AFTER_PUBLISH" if switched else "FAILED",
                      checked_at=datetime.now(timezone.utc).isoformat(), error=f"{type(exc).__name__}: {exc}")
        if run_dir.is_dir():
            atomic_write_json(run_dir / "status.json", status)
        atomic_write_json(workspace / "status.json", status)
        raise
    finally:
        lock.unlink()

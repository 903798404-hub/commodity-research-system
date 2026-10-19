"""Replay official FX history and independent latest-publication references."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from .foreign_fx import BY_CODE, ECB_HISTORY_URL, FxDataError, bcb_url, parse_bcb, parse_ecb, validate_snapshot
from .foreign_fx_update import MAX_RESPONSE_BYTES

BCB_LEGACY_LATEST_URL = "https://ptax.bcb.gov.br/ptax_internet/consultarUltimaCotacaoDolar.do"
BCB_LATEST_URL = "https://www.bcb.gov.br/api/servico/sitebcb/indicadorCambio"
ECB_LATEST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
EVIDENCE_SCHEMA = "foreign-fx-source-evidence/1"
WINDOW_EVIDENCE_SCHEMA = "foreign-fx-source-evidence/2"
ECB_RECENT_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
REVISION_LOOKBACK_DAYS = 14
START = date(2021, 1, 1)
PTAX_HISTORY_BASE = ('https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/'
                     'CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)')


def ptax_url(start, end):
    return PTAX_HISTORY_BASE + '?' + urlencode({
        '@dataInicial': start.strftime("'%m-%d-%Y'"),
        '@dataFinalCotacao': end.strftime("'%m-%d-%Y'"), '$top': '10000', '$format': 'json'})


def parse_ptax(raw, start, end):
    """BCB daily USD closing sale quote; never use an intraday currency bulletin."""
    def closed(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise FxDataError('BCB PTAX duplicate JSON field')
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode('utf-8-sig', errors='strict'), object_pairs_hook=closed)
        exact(value, ('@odata.context', 'value'), 'BCB PTAX response')
        if (not isinstance(value['@odata.context'], str)
                or not value['@odata.context'].endswith('/odata$metadata#_CotacaoDolarPeriodo')
                or not isinstance(value['value'], list) or not 0 < len(value['value']) < 10000):
            raise FxDataError('BCB PTAX response identity or coverage invalid')
        result, days = [], set()
        for row in value['value']:
            exact(row, ('cotacaoCompra', 'cotacaoVenda', 'dataHoraCotacao'), 'BCB PTAX daily quote')
            stamp = datetime.fromisoformat(row['dataHoraCotacao'])
            day = (stamp.astimezone(ZoneInfo('America/Sao_Paulo')).date()
                   if stamp.tzinfo is not None else stamp.date())
            sale = row['cotacaoVenda']
            if (day in days or not start <= day <= end or type(sale) not in (float, int)
                    or not 0 < float(sale) < float('inf')):
                raise FxDataError('BCB PTAX duplicate date, range or sale quote invalid')
            days.add(day)
            # Stable logical series ID, not the acquisition-channel identifier.
            # Exact official PTAX URL and unmodified bytes are recorded separately.
            result.append(dict(currency='BRL', date=day.isoformat(),
                               local_per_usd=float(sale), provider='BCB_SGS1'))
        return sorted(result, key=lambda r: r['date'])
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, FxDataError): raise
        raise FxDataError('BCB PTAX JSON or closing quote invalid') from exc


class FxSourceError(FxDataError):
    """Bounded diagnostics for an official request, without response bodies."""
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def failure_details(exc, stage):
    message = re.sub(r"(?i)(token|password|api[_-]?key|authorization)(\s*[=:]\s*)[^\s&]+",
                     r"\1\2[REDACTED]", str(exc))
    message = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", message)
    result = dict(stage=stage, error_type=type(exc).__name__, message=message[:1000])
    if isinstance(exc, FxSourceError):
        result.update(exc.details)
    return result


def fetch_delivery_official(url):
    """Use verified direct TLS, independently of desktop proxy/CA environment."""
    import requests
    allowed = {"api.bcb.gov.br", "www.bcb.gov.br", "www.ecb.europa.eu", "olinda.bcb.gov.br"}
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in allowed:
        raise FxDataError("FX official URL outside allowlist")
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.get(url, headers={"User-Agent": "AgriculturalResearch/1.0"}, timeout=45,
                             stream=True, allow_redirects=False) as response:
                if response.status_code != 200 or response.url != url:
                    raise FxSourceError("FX official response status or destination invalid",
                                        source_url=url, http_status=response.status_code)
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise FxSourceError("FX official response exceeds size limit", source_url=url,
                                            http_status=response.status_code, received_bytes=len(raw))
        except requests.RequestException as exc:
            details=failure_details(exc, "request")
            raise FxSourceError("FX official request failed", source_url=url,
                                cause_type=details['error_type'], cause_message=details['message']) from exc
    return bytes(raw)


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


def _legacy_bcb_latest(raw):
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


def bcb_latest(raw):
    """Only USD closing sale quotes; intermediate and EUR quotes are not anchors."""
    def closed(pairs):
        value={}
        for key,item in pairs:
            if key in value: raise FxDataError("BCB latest response has duplicate fields")
            value[key]=item
        return value
    try:
        value=json.loads(raw.decode('utf-8',errors='strict'),object_pairs_hook=closed)
        rows=value['conteudo']
        if not isinstance(rows,list) or not 0<len(rows)<=100:
            raise FxDataError("BCB latest closing rows missing or oversized")
        quotes=[row for row in rows if isinstance(row,dict) and row.get('moeda')=='Dólar'
                and row.get('tipoCotacao')=='Fechamento']
        if len(quotes)!=1:
            raise FxDataError("BCB latest closing sale quote missing or ambiguous")
        quote=quotes[0]
        timestamp=datetime.fromisoformat(quote['dataIndicador'])
        if timestamp.tzinfo is None: raise FxDataError("BCB closing timestamp lacks timezone")
        value=quote['valorVenda']
        if type(value) not in (int,float) or not 0<float(value)<float('inf'):
            raise FxDataError("BCB latest closing sale quote invalid")
        return timestamp.astimezone(ZoneInfo('America/Sao_Paulo')).date().isoformat(),float(value)
    except (UnicodeError,json.JSONDecodeError,KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,FxDataError):raise
        raise FxDataError("BCB latest JSON or closing quote invalid") from exc


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


def _bcb_capture(record, start, end):
    url = record.get('url') if isinstance(record, dict) else None
    if url == bcb_url(start, end): parser = parse_bcb
    elif url == ptax_url(start, end): parser = parse_ptax
    else: raise FxDataError('BCB history official URL or date window differs')
    raw = _decode(record, url)
    return raw, parser(raw, start, end), url


def _fetch_bcb(start, end, raw_directory, fetcher, baseline):
    """Use an official same-fixing fallback only after exact overlap agreement."""
    url = bcb_url(start, end)
    try:
        raw = fetcher(url)
        (raw_directory / 'bcb_history.raw').write_bytes(raw)
        parse_bcb(raw, start, end)
        return url, raw
    except (FxDataError, UnicodeError, json.JSONDecodeError) as exc:
        if baseline is None: raise
        failure = failure_details(exc, 'bcb_sgs_history')
        failure['source_url'] = url
        primary = raw_directory / 'bcb_history.raw'
        if primary.exists():
            body = primary.read_bytes()
            (raw_directory / 'bcb_history_primary_failed.raw').write_bytes(body)
            failure.update(response_sha256=hashlib.sha256(body).hexdigest(), received_bytes=len(body))
        (raw_directory / 'bcb_primary_failure.json').write_bytes(canonical(failure))
    url = ptax_url(start, end)
    raw = fetcher(url)
    (raw_directory / 'bcb_ptax.raw').write_bytes(raw)
    parsed = parse_ptax(raw, start, end)
    old = {r['date']: r for r in baseline['observations']
           if r['currency'] == 'BRL' and start.isoformat() <= r['date'] <= end.isoformat()}
    current = {r['date']: r for r in parsed}
    if not old or any(current.get(day) != row for day, row in old.items()):
        raise FxDataError('BCB PTAX fallback differs from published SGS1 overlap; review required')
    (raw_directory / 'bcb_source_selection.json').write_bytes(canonical(dict(
        channel='BCB_PTAX_OFFICIAL_FALLBACK', logical_series='BCB_SGS1', source_url=url,
        overlap_dates_checked=len(old), overlap_changed=0, primary_failure=failure)))
    return url, raw


def collect(end: date, raw_directory: Path, *, fetcher=fetch_delivery_official, baseline=None):
    if type(end) is not date or not START <= end <= date.today():
        raise FxDataError('FX collection request date invalid')
    raw_directory.mkdir(parents=True, exist_ok=True)
    bcb_history_url, bcb_raw = _fetch_bcb(START, end, raw_directory, fetcher, baseline)
    bcb_parser = parse_bcb if bcb_history_url == bcb_url(START, end) else parse_ptax
    ecb_raw = fetcher(ECB_HISTORY_URL)
    (raw_directory / 'ECB_CROSS.xml').write_bytes(ecb_raw)
    rows = bcb_parser(bcb_raw, START, end) + parse_ecb(ecb_raw, START, end)
    now = datetime.now().astimezone().isoformat()
    snapshot = dict(schema_version='foreign-fx-daily/1', generated_at=now,
        sources=[dict(provider=provider, url=url, raw_sha256=hashlib.sha256(raw).hexdigest(),
                      retrieved_at=now, method=method) for provider, url, raw, method in [
            ('BCB_SGS1', bcb_history_url, bcb_raw, 'PTAX卖出参考汇率'),
            ('ECB_CROSS', ECB_HISTORY_URL, ecb_raw, '同日EUR/本币除以EUR/USD')]],
        observations=sorted(rows, key=lambda row: (row['currency'], row['date'])))
    references = {"bcb_latest": (BCB_LATEST_URL, fetcher(BCB_LATEST_URL)),
                  "ecb_latest": (ECB_LATEST_URL, fetcher(ECB_LATEST_URL))}
    for key, (_url, raw) in references.items():
        (raw_directory / (key + ".raw")).write_bytes(raw)
    evidence = {"schema_version": EVIDENCE_SCHEMA, "request_start": START.isoformat(),
                "request_end": end.isoformat(), "raw": {
                    "bcb_history": _raw_record(bcb_history_url, bcb_raw),
                    "ecb_history": _raw_record(ECB_HISTORY_URL, (raw_directory / "ECB_CROSS.xml").read_bytes()),
                    **{key: _raw_record(url, raw) for key, (url, raw) in references.items()}}}
    observations(snapshot, None, evidence)
    return snapshot, evidence


def replay(snapshot, evidence):
    if evidence.get("schema_version") == WINDOW_EVIDENCE_SCHEMA:
        return _replay_windows(snapshot, evidence)
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
    latest_url=records['bcb_latest'].get('url') if isinstance(records['bcb_latest'],dict) else None
    if latest_url not in (BCB_LATEST_URL,BCB_LEGACY_LATEST_URL):
        raise FxDataError("BCB latest official URL differs")
    _, bcb_rows, _ = _bcb_capture(records['bcb_history'], START, end)
    raws = {key: _decode(records[key], url) for key, url in {
        "ecb_history": ECB_HISTORY_URL,
        "bcb_latest": latest_url, "ecb_latest": ECB_LATEST_URL}.items()}
    rows = sorted(bcb_rows + parse_ecb(raws["ecb_history"], START, end),
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
    expected_bcb, bcb_sale = (_legacy_bcb_latest if latest_url==BCB_LEGACY_LATEST_URL else bcb_latest)(raws["bcb_latest"])
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


def _window_dates(segment):
    exact(segment, ("start", "end", "raw", "retrieved_at"), "FX window")
    start, end = date.fromisoformat(segment["start"]), date.fromisoformat(segment["end"])
    if start.isoformat() != segment["start"] or end.isoformat() != segment["end"] or not START <= start <= end:
        raise FxDataError("FX window dates invalid")
    return start, end


def _window_rows(segments):
    rows, identities, sources = {}, {"BCB_SGS1": [], "ECB_CROSS": []}, {}
    covered_until = None
    for segment in segments:
        start, end = _window_dates(segment)
        if covered_until is None and start != START:
            raise FxDataError("FX history anchor missing")
        if covered_until is not None and (end < covered_until or start > covered_until + timedelta(days=1)):
            raise FxDataError("FX windows retreat or leave a gap")
        records = exact(segment["raw"], ("bcb_history", "ecb_history"), "FX window raw")
        captures = exact(segment["retrieved_at"], ("BCB_SGS1", "ECB_CROSS"), "FX window capture times")
        for captured in captures.values():
            if datetime.fromisoformat(captured).tzinfo is None:
                raise FxDataError("FX window capture time lacks timezone")
        ecb_url = records["ecb_history"].get("url")
        if ecb_url not in {ECB_HISTORY_URL, ECB_RECENT_URL}:
            raise FxDataError("FX window ECB URL invalid")
        fresh = {}
        bcb_url_value = records['bcb_history'].get('url')
        if bcb_url_value not in {bcb_url(start, end), ptax_url(start, end)}:
            raise FxDataError('BCB window official URL differs')
        bcb_parser = parse_bcb if bcb_url_value == bcb_url(start, end) else parse_ptax
        for provider, key, url, parser in (("BCB_SGS1", "bcb_history", bcb_url_value, bcb_parser),
                                          ("ECB_CROSS", "ecb_history", ecb_url, parse_ecb)):
            raw = _decode(records[key], url)
            parsed = parser(raw, start, end)
            if len({(r["currency"], r["date"]) for r in parsed}) != len(parsed):
                raise FxDataError("FX source has duplicate business keys")
            fresh.update({(r["currency"], r["date"]): r for r in parsed})
            identities[provider].append({"start": segment["start"], "end": segment["end"],
                                         "url": url, "sha256": records[key]["sha256"], "retrieved_at": captures[provider]})
            sources[provider] = url
        expected = {key for key in rows if start.isoformat() <= key[1] <= end.isoformat()}
        if expected - set(fresh):
            raise FxDataError("FX refresh removes published historical observations")
        for day in {r["date"] for r in fresh.values() if r["provider"] == "ECB_CROSS"}:
            if {r["currency"] for r in fresh.values() if r["date"] == day and r["provider"] == "ECB_CROSS"} != set(BY_CODE) - {"BRL"}:
                raise FxDataError("ECB window has partial currency coverage")
        rows.update(fresh)
        covered_until = end
    return sorted(rows.values(), key=lambda r: (r["currency"], r["date"])), identities, sources, covered_until


def _replay_windows(snapshot, evidence):
    exact(snapshot, ("schema_version", "generated_at", "sources", "observations"), "FX snapshot")
    validate_snapshot(snapshot)
    exact(evidence, ("schema_version", "request_start", "request_end", "segments", "raw"), "FX window evidence")
    if evidence["request_start"] != START.isoformat() or not isinstance(evidence["segments"], list) or not 1 <= len(evidence["segments"]) <= 400:
        raise FxDataError("FX window evidence bounds invalid")
    rows, identities, urls, end = _window_rows(evidence["segments"])
    if end.isoformat() != evidence["request_end"] or rows != snapshot["observations"]:
        raise FxDataError("FX observations differ from window replay")
    sources = {s["provider"]: s for s in snapshot["sources"]}
    if set(sources) != set(identities):
        raise FxDataError("FX window provider coverage differs")
    for provider, source in sources.items():
        exact(source, ("provider", "url", "raw_sha256", "retrieved_at", "method"), "FX source")
        if source["url"] != urls[provider] or source["raw_sha256"] != hashlib.sha256(canonical(identities[provider])).hexdigest():
            raise FxDataError("FX window source identity differs")
        expected_method = "PTAX卖出参考汇率" if provider == "BCB_SGS1" else "同日EUR/本币除以EUR/USD"
        if source["method"] != expected_method or datetime.fromisoformat(source["retrieved_at"]).tzinfo is None:
            raise FxDataError("FX window method or timestamp differs")
    anchors = exact(evidence["raw"], ("bcb_latest", "ecb_latest"), "FX window anchors")
    bcb_day, bcb_price = bcb_latest(_decode(anchors["bcb_latest"], BCB_LATEST_URL))
    ecb = parse_ecb(_decode(anchors["ecb_latest"], ECB_LATEST_URL), START, end)
    if len({r["date"] for r in ecb}) != 1 or {r["currency"] for r in ecb} != set(BY_CODE) - {"BRL"}:
        raise FxDataError("ECB latest reference has partial coverage")
    latest = {code: max(r["date"] for r in rows if r["currency"] == code) for code in BY_CODE}
    if latest["BRL"] != bcb_day or any(latest[r["currency"]] != r["date"] for r in ecb):
        raise FxDataError("History has not reached the official latest publication; retry required")
    index = {(r["currency"], r["date"]): r for r in rows}
    if index[("BRL", bcb_day)]["local_per_usd"] != bcb_price or any(index[(r["currency"], r["date"])] != r for r in ecb):
        raise FxDataError("FX latest fixing differs from history")
    return rows, latest


def collect_incremental(end, raw_directory, *, baseline=None, baseline_evidence=None,
                        fetcher=fetch_delivery_official, reconcile=False):
    """Refresh a bounded overlap, replay retained raw windows, reconcile monthly."""
    if type(end) is not date or not START <= end <= date.today():
        raise FxDataError("FX incremental request date invalid")
    if baseline is None:
        if baseline_evidence is not None:
            raise FxDataError("FX evidence lacks a baseline")
        return collect(end, raw_directory, fetcher=fetcher)
    if baseline_evidence is None:
        raise FxDataError("FX baseline evidence missing")
    _, latest = replay(baseline, baseline_evidence)
    previous_end = date.fromisoformat(baseline_evidence["request_end"])
    if end < previous_end:
        raise FxDataError("FX request end retreats")
    if reconcile or (end.year, end.month) != (previous_end.year, previous_end.month):
        snapshot, evidence = collect(end, raw_directory, fetcher=fetcher, baseline=baseline)
        observations(snapshot, baseline, evidence)
        return snapshot, evidence
    start = max(START, min(date.fromisoformat(v) for v in latest.values()) - timedelta(days=REVISION_LOOKBACK_DAYS))
    ecb_url = ECB_RECENT_URL if start >= end - timedelta(days=85) else ECB_HISTORY_URL
    segment = {"start": start.isoformat(), "end": end.isoformat(), "raw": {}, "retrieved_at": {}}
    raw_directory.mkdir(parents=True, exist_ok=True)
    bcb_history_url, bcb_raw = _fetch_bcb(start, end, raw_directory, fetcher, baseline)
    for key, url in (("bcb_history", bcb_history_url), ("ecb_history", ecb_url)):
        raw = bcb_raw if key == 'bcb_history' else fetcher(url)
        (raw_directory / (key + ".raw")).write_bytes(raw)
        segment["raw"][key] = _raw_record(url, raw)
        segment["retrieved_at"]["BCB_SGS1" if key == "bcb_history" else "ECB_CROSS"] = datetime.now().astimezone().isoformat()
    if baseline_evidence["schema_version"] == EVIDENCE_SCHEMA:
        segments = [{"start": START.isoformat(), "end": previous_end.isoformat(),
                     "raw": {key: deepcopy(baseline_evidence["raw"][key]) for key in ("bcb_history", "ecb_history")},
                     "retrieved_at": {s["provider"]: s["retrieved_at"] for s in baseline["sources"]}}]
    else:
        segments = deepcopy(baseline_evidence["segments"])
    segments.append(segment)
    evidence = {"schema_version": WINDOW_EVIDENCE_SCHEMA, "request_start": START.isoformat(),
                "request_end": end.isoformat(), "segments": segments, "raw": {}}
    for key, url in (("bcb_latest", BCB_LATEST_URL), ("ecb_latest", ECB_LATEST_URL)):
        raw = fetcher(url); (raw_directory / (key + ".raw")).write_bytes(raw)
        evidence["raw"][key] = _raw_record(url, raw)
    rows, identities, urls, _ = _window_rows(segments)
    sources = [{**source, "url": urls[source["provider"]],
                "raw_sha256": hashlib.sha256(canonical(identities[source["provider"]])).hexdigest(),
                "retrieved_at": datetime.now().astimezone().isoformat()} for source in baseline["sources"]]
    snapshot = {**baseline, "generated_at": datetime.now().astimezone().isoformat(), "sources": sources, "observations": rows}
    observations(snapshot, baseline, evidence)
    return snapshot, evidence


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

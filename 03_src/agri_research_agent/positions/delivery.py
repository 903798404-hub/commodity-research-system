"""Bounded source-backed archives for the protected positioning data domain.

No network, source execution, production writes, or preview fallback occurs here.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit
import io
import zipfile

SCHEMA = "commodity-positions-archive/1"
DOMAINS = ("sugar", "rapeseed", "soybean", "palm")
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILES = 4096
MAX_DOCUMENT = 384 * 1024 * 1024
RELEASE = re.compile(r"[0-9TZ_-]+[a-f0-9]{8}")
SHA = re.compile(r"[a-f0-9]{64}")
EM_URL = "https://qhweb.eastmoney.com/lhb/dkcc/dce"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def strict_json(raw):
    require(len(raw) <= MAX_DOCUMENT, "positions JSON size bound exceeded")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate positions JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("nonfinite positions JSON number")
    def number(value):
        result = float(value)
        require(math.isfinite(result), "nonfinite positions JSON number")
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)


def read_json(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_DOCUMENT, "positions JSON size bound exceeded")
    return strict_json(path.read_bytes())


def exact(value, keys, label):
    require(type(value) is dict and set(value) == set(keys), f"{label} fields invalid")
    return value


def unlinked(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        require(not item.is_symlink() and not getattr(item, "is_junction", lambda: False)(), "positions path redirects")
    return path


def relative(value):
    require(type(value) is str and bool(value) and "\\" not in value and ":" not in value,
            "positions archive path invalid")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value and
            not set(path.parts) & {".", ".."}, "positions archive path invalid")
    return path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(files):
    require(0 < len(files) <= MAX_FILES, "positions file count bound exceeded")
    total = 0
    result = {}
    for name, raw in files.items():
        relative(name)
        require(type(raw) is bytes and 0 < len(raw) <= MAX_FILE, "positions file size bound exceeded")
        total += len(raw)
        require(total <= MAX_TOTAL, "positions total size bound exceeded")
        result[name] = {"sha256": digest(raw), "size_bytes": len(raw),
                        "bytes_base64": base64.b64encode(raw).decode("ascii")}
    return {"schema_version": SCHEMA, "files": result}


def decode(archive):
    exact(archive, ("schema_version", "files"), "positions archive")
    require(archive["schema_version"] == SCHEMA, "positions archive schema invalid")
    entries = archive["files"]
    require(type(entries) is dict and 0 < len(entries) <= MAX_FILES, "positions file count bound exceeded")
    result, total = {}, 0
    for name, entry in entries.items():
        parts = relative(name).parts
        require(len(parts) >= 2 and parts[0] in DOMAINS, "positions domain path invalid")
        exact(entry, ("sha256", "size_bytes", "bytes_base64"), "positions file")
        size, packed = entry["size_bytes"], entry["bytes_base64"]
        require(type(size) is int and 0 < size <= MAX_FILE, "positions file size bound exceeded")
        total += size
        require(total <= MAX_TOTAL, "positions total size bound exceeded")
        require(type(packed) is str and len(packed) == 4 * ((size + 2) // 3),
                "positions base64 size bound exceeded")
        require(type(entry["sha256"]) is str and SHA.fullmatch(entry["sha256"]), "positions SHA invalid")
        raw = base64.b64decode(packed, validate=True)
        require(len(raw) == size and digest(raw) == entry["sha256"], "positions file identity differs")
        result[name] = raw
    return result


def _xlsx_bound(raw):
    # The external file-size bound also needs a bound on XLSX ZIP expansion.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) <= MAX_FILES and sum(x.file_size for x in entries) <= MAX_TOTAL
                and all(x.file_size <= MAX_FILE for x in entries), "positions XLSX size bound exceeded")


def _replay_source(key, source, raw, domain, spec):
    # Business dependencies are only loaded in the producer / fixed image worker.
    # The protected Linux host's codec and stage materializer remain stdlib-only.
    from agri_research_agent.oilseed_positions.aggregation import parse_browser_capture
    from agri_research_agent.oilseed_positions.sources import DCE_URL, parse_euronext, parse_sina, parse_dce
    from agri_research_agent.sugar_positions.sources import parse_cftc, parse_ice, parse_czce
    url, stamp = source["url"], source["retrieved_at"]
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc), "positions source timestamp invalid")
    varieties = ("SR",) if domain == "sugar" else tuple(spec[domain]["domestic"])
    if key.startswith('cftc_archive_'):
        from agri_research_agent.positions.cftc_annual import archive_url,parse_archive
        match=re.fullmatch(r'cftc_archive_(20\d{2})_(sugar11|canola|cbot_soybean|cbot_meal|cbot_oil)_(futures_only|combined)',key)
        require(match is not None,'positions CFTC archive key invalid')
        year,market,suffix=match.groups();year=int(year)
        require((domain=='sugar' and market=='sugar11') or (domain!='sugar' and market in spec[domain]['foreign']),
                'positions CFTC archive domain differs')
        definition={'code':'080732','name':'SUGAR NO. 11'} if domain=='sugar' else spec[domain]['foreign'][market]
        require(url==archive_url(year,suffix),'positions CFTC archive URL invalid')
        return 'foreign',parse_archive(raw,year,suffix,market,definition['code'],definition['name'],stamp)
    if key.startswith("cftc_"):
        suffix = "combined" if key.endswith("_combined") else "futures_only"
        require(key.endswith("_" + suffix), "positions CFTC source key invalid")
        market = "sugar11" if domain == "sugar" else key[len("cftc_"):-len(suffix)-1]
        code = "080732" if domain == "sugar" else spec[domain]["foreign"][market]["code"]
        require(key == ("cftc_" if domain == "sugar" else "cftc_" + market + "_") + suffix,
                "positions CFTC source identity invalid")
        uri = urlsplit(url)
        dataset = "kh3c-gbw2" if suffix == "combined" else "72hh-3qpy"
        query = parse_qs(uri.query, strict_parsing=True)
        require(uri.scheme == "https" and uri.netloc == "publicreporting.cftc.gov" and
                uri.path == f"/resource/{dataset}.json" and not uri.fragment and
                set(query) == {"cftc_contract_market_code", "$limit", "$where", "$order"} and
                query["cftc_contract_market_code"] == [code] and query["$limit"] == ["10000"] and
                query["$order"] == ["report_date_as_yyyy_mm_dd ASC"] and len(query["$where"]) == 1 and
                re.fullmatch(r"report_date_as_yyyy_mm_dd >= '20\d{2}-01-01T00:00:00'", query["$where"][0]),
                "positions CFTC URL invalid")
        kwargs = {} if domain == "sugar" else {"market": market, "expected_code": code,
                                                "expected_name": spec[domain]["foreign"][market]["name"]}
        return "foreign", parse_cftc(strict_json(raw), suffix, url, stamp, **kwargs)
    if key.startswith("ice_"):
        year = int(key[4:])
        require(domain == "sugar" and url == f"https://www.ice.com/publicdocs/futures/COTHist{year}.csv",
                "positions ICE URL invalid")
        return "foreign", parse_ice(raw, year, url, stamp)
    if key.startswith("euronext_"):
        day = datetime.strptime(key[9:], "%Y%m%d").date()
        require(domain == "rapeseed" and url == day.strftime(
            "https://live.euronext.com/sites/default/files/commodities_reporting/%Y/%m/%d/en/cdwpr_ECO_%Y%m%d.html"),
            "positions Euronext URL invalid")
        return "foreign", parse_euronext(raw, day, url, stamp)
    if key.startswith("czce_"):
        day = datetime.strptime(key[5:], "%Y%m%d").date()
        require(domain in {"sugar", "rapeseed"} and url == day.strftime(
            "https://www.czce.com.cn/cn/DFSStaticFiles/Future/%Y/%Y%m%d/FutureDataHolding.xlsx"),
            "positions CZCE URL invalid")
        _xlsx_bound(raw)
        return "domestic", parse_czce(raw, day.isoformat(), url, stamp, varieties=varieties)
    if key.startswith('stockapi_DCE_'):
        from agri_research_agent.oilseed_positions.stock_api import parse_capture, URL
        compact, *contracts = key.removeprefix('stockapi_DCE_').split('_')
        require(len(contracts) <= 1, 'positions stock-api key invalid')
        day = datetime.strptime(compact, '%Y%m%d').date()
        require(domain in {'soybean', 'palm'} and url == URL, 'positions stock-api source invalid')
        capture = strict_json(raw)
        require(capture['trade_date'] == day.strftime('%Y%m%d'), 'positions stock-api date differs')
        require(capture.get('scope_contract') == (contracts[0] if contracts else None),
                'positions stock-api scope differs')
        rows = parse_capture(raw, url, stamp)
        return 'domestic', [r for r in rows if re.sub(r'\d+$', '', r['scope']) in varieties]
    if key.startswith("sina_"):
        _, contract, compact = key.split("_")
        day = datetime.strptime(compact, "%Y%m%d").date()
        require(domain in {"soybean", "palm"} and re.sub(r"\d+$", "", contract) in varieties and
                url == "https://vip.stock.finance.sina.com.cn/q/view/vFutures_Positions_cjcc.php?"
                       f"t_breed={contract}&t_date={day.isoformat()}", "positions Sina URL invalid")
        return "domestic", parse_sina(raw, day, contract, url, stamp)
    if key.startswith("browser_contracts_"):
        day = datetime.strptime(key.removeprefix("browser_contracts_" + domain + "_"), "%Y%m%d").date()
        require(domain in {"soybean", "palm"} and url == EM_URL, "positions browser source invalid")
        capture = strict_json(raw)
        require(capture["report_date"] == day.isoformat(), "positions browser date differs")
        for report in capture["reports"]:
            require(report["source_url"] == EM_URL + "/" + report["contract"].lower(),
                    "positions browser URL invalid")
        rows, _ = parse_browser_capture(raw, varieties)
        return "domestic", rows
    if key.startswith("dce_"):
        day = datetime.strptime(key[4:], "%Y%m%d").date()
        require(domain in {"soybean", "palm"} and url == DCE_URL,
                "positions DCE URL invalid")
        _xlsx_bound(raw)
        return "domestic", parse_dce(raw, day, varieties, url, stamp)
    raise ValueError("positions source kind unsupported")


def _part(row, kind):
    fields = ("market", "report_type", "report_date") if kind == "foreign" else ("scope", "report_date")
    return tuple(row[k] for k in fields)


def _groups(rows, kind):
    result = defaultdict(set)
    for row in rows:
        result[_part(row, kind)].add(canonical({k: v for k, v in row.items() if k != "retrieved_at"}))
    return dict(result)


def validate_archive(archive, project_root):
    from agri_research_agent.positions.workspace import validate_domain
    from agri_research_agent.oilseed_positions.model import config
    from agri_research_agent.sugar_positions.storage import DOMESTIC_KEY, FOREIGN_KEY
    from agri_research_agent.sugar_positions.model import unique_rows
    files = decode(archive)
    require(all(domain + "/current.json" in files for domain in DOMAINS), "positions archive missing board")
    spec = config(project_root)
    expected, snapshots = set(), {}
    for domain in DOMAINS:
        prefix = domain + "/"
        pointer = exact(strict_json(files[prefix + "current.json"]), ("release_id", "manifest_sha256"), "pointer")
        release = pointer["release_id"]
        require(type(release) is str and RELEASE.fullmatch(release), "positions release invalid")
        base = prefix + "releases/" + release + "/"
        manifest_raw, snapshot_raw = files[base + "manifest.json"], files[base + "snapshot.json"]
        manifest = exact(strict_json(manifest_raw), ("schema_version", "release_id", "snapshot_sha256", "foreign_rows", "domestic_rows"), "manifest")
        require(digest(manifest_raw) == pointer["manifest_sha256"] and digest(snapshot_raw) == manifest["snapshot_sha256"]
                and manifest["schema_version"] == 1 and manifest["release_id"] == release, "positions release identity differs")
        snapshot = exact(strict_json(snapshot_raw), ("schema_version", "release_id", "published_at", "foreign", "domestic", "sources", "attempts"), "snapshot")
        require(snapshot["schema_version"] == 1 and snapshot["release_id"] == release and
                type(snapshot["foreign"]) is list and type(snapshot["domestic"]) is list and
                type(snapshot["sources"]) is dict and type(snapshot["attempts"]) is list and
                bool(snapshot["foreign"] or snapshot["domestic"]), "positions snapshot invalid")
        published = datetime.fromisoformat(snapshot["published_at"].replace("Z", "+00:00"))
        require(published.tzinfo is not None and published <= datetime.now(timezone.utc), "positions publication time invalid")
        validate_domain(snapshot, project_root, domain)
        expected.update((prefix + "current.json", base + "manifest.json", base + "snapshot.json"))
        replayed = {"foreign": defaultdict(list), "domestic": defaultdict(list)}
        for key, source in snapshot["sources"].items():
            exact(source, ("url", "retrieved_at", "sha256", "raw_file"), "source")
            require(type(source["sha256"]) is str and SHA.fullmatch(source["sha256"]), "positions source SHA invalid")
            path = relative(source["raw_file"])
            require(len(path.parts) == 2 and path.parts[0] == "raw" and
                    re.fullmatch(source["sha256"] + r"\.(?:csv|html|xlsx|json|zip)", path.parts[1]),
                    "positions raw path invalid")
            name = prefix + str(path)
            expected.add(name)
            raw = files[name]
            require(digest(raw) == source["sha256"], "positions raw SHA differs")
            kind, rows = _replay_source(key, source, raw, domain, spec)
            # Each report partition must match one complete parsed capture.
            # Keeping older Sina evidence alongside newer browser evidence must
            # neither mix both rankings nor permit dropping a subset of members.
            for part, values in _groups(rows, kind).items():
                replayed[kind][part].append(values)
        for kind, business_key in (("foreign", FOREIGN_KEY), ("domestic", DOMESTIC_KEY)):
            rows = snapshot[kind]
            unique_rows(rows, business_key)
            require(manifest[kind + "_rows"] == len(rows), "positions row count differs")
            for row in rows:
                day = date.fromisoformat(row["report_date"])
                require(day.isoformat() == row["report_date"] and day <= datetime.now(timezone.utc).date(),
                        "positions report date invalid")
            actual = _groups(rows, kind)
            require(all(values in replayed[kind].get(part, []) for part, values in actual.items()), "positions source replay differs")
        attempt = prefix + "last_attempt.json"
        if attempt in files:
            require(type(strict_json(files[attempt])) is dict, "positions attempt invalid")
            expected.add(attempt)
        snapshots[domain] = snapshot
    require(set(files) == expected, "positions archive contains unexpected files")
    return snapshots, files


def observations(candidate, baseline, project_root):
    snapshots, _ = validate_archive(candidate, project_root)
    previous = validate_archive(baseline, project_root)[0] if baseline is not None else {}
    added, revised, total, dates = [], [], 0, {}
    for domain, snapshot in snapshots.items():
        for kind in ("foreign", "domestic"):
            current = _groups(snapshot[kind], kind)
            old = _groups(previous[domain][kind], kind) if previous else {}
            require(not set(old) - set(current), "positions candidate drops historical report partitions")
            for part, values in current.items():
                label = "/".join((domain, kind, *part))
                if part not in old:
                    added.append(label)
                elif values != old[part]:
                    revised.append(label)
                series = "/".join((domain, kind, *part[:-1]))
                dates[series] = max(dates.get(series, ""), part[-1])
            total += len(snapshot[kind])
    return {"record_count": total, "latest_dates": dict(sorted(dates.items())),
            "added_partitions": sorted(added), "revised_partitions": sorted(revised),
            "business_changed": bool(added or revised)}


def archive_bundle(root, project_root):
    root = unlinked(root)
    manifest_raw = unlinked(root / "bundle.json").read_bytes()
    manifest = exact(strict_json(manifest_raw), ("schema_version", "domains", "files"), "export bundle")
    require(manifest["schema_version"] == "commodity-positions-bundle/1" and
            type(manifest["domains"]) is dict and set(manifest["domains"]) == set(DOMAINS) and
            type(manifest["files"]) is dict and 0 < len(manifest["files"]) <= MAX_FILES, "positions export bundle invalid")
    files, total = {}, 0
    for rel, sha in manifest["files"].items():
        path = relative(rel)
        require(path.parts[0] == "domains" and len(path.parts) > 2, "positions export path invalid")
        source = unlinked(root / rel)
        size = source.stat().st_size
        total += size
        require(0 < size <= MAX_FILE and total <= MAX_TOTAL, "positions export size bound exceeded")
        raw = source.read_bytes()
        require(digest(raw) == sha, "positions export SHA differs")
        files[PurePosixPath(*path.parts[1:]).as_posix()] = raw
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if unlinked(p).is_file()}
    require(actual == {"bundle.json", *manifest["files"]}, "positions export contains unexpected files")
    archive = encode(files)
    snapshots, _ = validate_archive(archive, project_root)
    for domain, snapshot in snapshots.items():
        require(manifest["domains"][domain] == {"release_id": snapshot["release_id"],
                "foreign_rows": len(snapshot["foreign"]), "domestic_rows": len(snapshot["domestic"])},
                "positions export domain identity differs")
    require((root / "bundle.json").read_bytes() == manifest_raw, "positions export changed during read")
    return archive


def verify_materialized(archive, root):
    """Pin the page's real inputs, beyond the transport archive's own hash."""
    root = unlinked(root)
    for name, raw in decode(archive).items():
        require(unlinked(root / name).read_bytes() == raw, "positions materialized input differs")


def materialize(archive, root):
    """Only for a publisher-owned stage; immutable histories cannot be overwritten."""
    root = unlinked(root)
    for name, raw in decode(archive).items():
        path = unlinked(root / name)
        if path.exists() and PurePosixPath(name).parts[1] not in {"current.json", "last_attempt.json"}:
            require(path.read_bytes() == raw, "positions immutable history differs")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        require(path.read_bytes() == raw, "positions stage write differs")


INCREMENT_SCHEMA = "commodity-positions-increment/1"


def archive_snapshots(archive):
    """Decode already pinned bytes; semantic/source replay remains a separate gate."""
    files = decode(archive)
    snapshots = {}
    for domain in DOMAINS:
        pointer = strict_json(files[domain + "/current.json"])
        snapshots[domain] = strict_json(files[domain + "/releases/" + pointer["release_id"] + "/snapshot.json"])
    return snapshots, files


def resolve_increment(document, baseline):
    """Expand an append-only transport in memory against its exact approved baseline.

    Legacy full archives remain readable. The host uses only stdlib here; its
    isolated validator subsequently replays every source and checks semantics.
    """
    if document.get("schema_version") == SCHEMA:
        return document
    exact(document, ("schema_version", "baseline_sha256", "updates", "objects"), "positions increment")
    require(document["schema_version"] == INCREMENT_SCHEMA and baseline is not None,
            "positions increment requires a baseline")
    require(digest(canonical(baseline)) == document["baseline_sha256"], "positions increment baseline differs")
    updates, objects = document["updates"], document["objects"]
    require(type(updates) is dict and set(updates) <= set(DOMAINS), "positions increment domains invalid")
    require(type(objects) is dict and len(objects) <= MAX_FILES, "positions increment objects invalid")
    decoded, total = {}, 0
    for sha, entry in objects.items():
        require(type(sha) is str and SHA.fullmatch(sha), "positions increment object key invalid")
        # Reuse the archive codec's strict size/base64/hash checks.
        raw = decode({"schema_version": SCHEMA, "files": {"sugar/raw/" + sha: entry}})["sugar/raw/" + sha]
        require(digest(raw) == sha, "positions increment object identity differs")
        decoded[sha] = raw
        total += len(raw)
        require(total <= MAX_TOTAL, "positions increment total size bound exceeded")
    snapshots, previous_files = archive_snapshots(baseline)
    retained_objects = {digest(raw): raw for name, raw in previous_files.items() if "/raw/" in name}
    files, used = dict(previous_files), set()
    for domain, update in updates.items():
        exact(update, ("release_id", "published_at", "foreign", "domestic", "sources", "attempts"), "positions increment update")
        require(type(update["sources"]) is dict and type(update["attempts"]) is list,
                "positions increment metadata invalid")
        old = snapshots[domain]
        snapshot = {**old, **update, "sources": {**old["sources"], **update["sources"]}}
        for kind in ("foreign", "domestic"):
            rows = update[kind]
            require(type(rows) is list and not (set(_groups(rows, kind)) & set(_groups(old[kind], kind))),
                    "positions increment overwrites historical partition")
            snapshot[kind] = old[kind] + rows
        require(update["foreign"] or update["domestic"], "empty positions increment update")
        release = snapshot["release_id"]
        require(type(release) is str and RELEASE.fullmatch(release) and release != old["release_id"],
                "positions increment release invalid")
        prefix = domain + "/"
        # The stable archive describes current inputs; immutable old releases and
        # raw evidence remain in the formal tree and its rollback backup.
        files = {name: raw for name, raw in files.items() if not name.startswith(prefix)}
        for source in snapshot["sources"].values():
            name = prefix + str(relative(source["raw_file"]))
            sha = source["sha256"]
            raw = previous_files.get(name, retained_objects.get(sha))
            if raw is None:
                require(sha in decoded, "positions increment evidence missing")
                raw = decoded[sha]; used.add(sha)
            require(digest(raw) == sha, "positions increment evidence differs")
            files[name] = raw
        raw = canonical(snapshot)
        manifest = canonical(dict(schema_version=1, release_id=release, snapshot_sha256=digest(raw),
                                  foreign_rows=len(snapshot["foreign"]), domestic_rows=len(snapshot["domestic"])))
        base = prefix + "releases/" + release + "/"
        files[base + "snapshot.json"] = raw
        files[base + "manifest.json"] = manifest
        files[prefix + "current.json"] = canonical(dict(release_id=release, manifest_sha256=digest(manifest)))
    require(used == set(objects), "positions increment contains unused objects")
    return encode(files)


class IncrementBuilder:
    """Accumulate only new complete report partitions, never a disk history clone."""

    def __init__(self, baseline, project_root):
        self.baseline = baseline
        self.snapshots, self.files = validate_archive(baseline, project_root)
        self.retained_hashes = {digest(raw) for name, raw in self.files.items() if "/raw/" in name}
        self.document = dict(schema_version=INCREMENT_SCHEMA, baseline_sha256=digest(canonical(baseline)),
                             updates={}, objects={})

    def publish(self, root, foreign, domestic, captures, attempts, **_kwargs):
        import uuid
        domain = Path(root).name
        require(domain in DOMAINS, "positions increment domain invalid")
        old = self.snapshots[domain]
        pending = self.document["updates"].get(domain)
        fresh = {}
        for kind, rows in (("foreign", foreign), ("domestic", domestic)):
            previous = _groups(old[kind] + (pending[kind] if pending else []), kind)
            current = _groups(rows, kind)
            require(all(previous[part] == values for part, values in current.items() if part in previous),
                    "positions historical revision requires review")
            fresh[kind] = [r for r in rows if _part(r, kind) not in previous]
        if not fresh["foreign"] and not fresh["domestic"]:
            return
        if pending is None:
            now = datetime.now(timezone.utc)
            pending = dict(release_id=now.strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8],
                           published_at=now.isoformat(), foreign=[], domestic=[], sources={}, attempts=[])
            self.document["updates"][domain] = pending
        for kind in ("foreign", "domestic"):
            pending[kind].extend(fresh[kind])
        pending["attempts"].extend(attempts)
        for key, raw, url, extension in captures:
            sha = digest(raw)
            name = "raw/" + sha + "." + extension
            pending["sources"][key] = dict(url=url, retrieved_at=datetime.now(timezone.utc).isoformat(),
                                          sha256=sha, raw_file=name)
            if sha not in self.retained_hashes:
                self.document["objects"][sha] = encode({domain + "/" + name: raw})["files"][domain + "/" + name]

    def finish(self, project_root):
        candidate = resolve_increment(self.document, self.baseline)
        semantic = observations(candidate, self.baseline, project_root)
        require(not semantic["revised_partitions"], "positions historical revision requires review")
        return self.document, semantic

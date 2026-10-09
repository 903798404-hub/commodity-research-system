"""Protected positioning delivery and independent-domain consumer regressions."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

from filelock import FileLock, Timeout
from jsonschema import Draft202012Validator, ValidationError
import pytest

from agri_research_agent.automation import production_data_delta_positions as producer
from agri_research_agent.positions import delivery
from agri_research_agent.oilseed_positions.model import config as domain_config
from agri_research_agent.oilseed_positions.sources import parse_sina
from agri_research_agent.sugar_positions.sources import parse_cftc

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-10-08T00:00:00Z"


def archive(day="2026-09-29", amount=20):
    files = {}
    specs = domain_config(ROOT)
    for domain in delivery.DOMAINS:
        foreign, domestic = [], []
        if domain == "palm":
            key = "sina_P2701_" + day.replace("-", "")
            url = "https://vip.stock.finance.sina.com.cn/q/view/vFutures_Positions_cjcc.php?t_breed=P2701&t_date=" + day
            text = f'<select name="t_breed"><option selected value="P2701">P2701</option></select><input name="t_date" value="{day}">'
            for title in ("多单持仓", "空单持仓"):
                text += f'<table><tr><th>名次</th><th>会员简称</th><th>{title}</th><th>比上交易增减</th></tr><tr><td>1</td><td>东证期货</td><td>100</td><td>-2</td></tr><tr><td>合计</td><td></td><td>100</td><td></td></tr></table>'
            raw = text.encode("gb18030")
            domestic = parse_sina(raw, date.fromisoformat(day), "P2701", url, STAMP)
            extension = "html"
        else:
            market = {"sugar": "sugar11", "rapeseed": "canola", "soybean": "cbot_soybean"}[domain]
            spec = {"code": "080732", "name": "SUGAR NO. 11 - ICE FUTURES U.S."} if domain == "sugar" else specs[domain]["foreign"][market]
            key = ("cftc_" if domain == "sugar" else "cftc_" + market + "_") + "futures_only"
            url = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json?cftc_contract_market_code=" + spec["code"] + "&%24limit=10000&%24order=report_date_as_yyyy_mm_dd+ASC&%24where=report_date_as_yyyy_mm_dd+%3E%3D+%272025-01-01T00%3A00%3A00%27"
            row = dict(cftc_contract_market_code=spec["code"], market_and_exchange_names=spec["name"],
                       report_date_as_yyyy_mm_dd=day + "T00:00:00", open_interest_all=str(amount * 5),
                       tot_rept_positions_long_all=str(amount * 4), tot_rept_positions_short=str(amount * 4))
            for field in ("prod_merc_positions_long", "prod_merc_positions_short", "swap_positions_long_all", "swap__positions_short_all", "m_money_positions_long_all", "m_money_positions_short_all", "other_rept_positions_long", "other_rept_positions_short", "nonrept_positions_long_all", "nonrept_positions_short_all"):
                row[field] = str(amount)
            for field in ("swap__positions_spread_all", "m_money_positions_spread", "other_rept_positions_spread"):
                row[field] = "0"
            raw = delivery.canonical([row])
            kwargs = {} if domain == "sugar" else {"market": market, "expected_code": spec["code"], "expected_name": spec["name"]}
            foreign = parse_cftc([row], "futures_only", url, STAMP, **kwargs)
            extension = "json"
        release = "20261008T000000Z_" + ("deadbeef" if amount == 20 else "cafebabe")
        raw_name = "raw/" + delivery.digest(raw) + "." + extension
        source = {"url": url, "retrieved_at": STAMP, "sha256": delivery.digest(raw), "raw_file": raw_name}
        snapshot = dict(schema_version=1, release_id=release, published_at=STAMP, foreign=foreign,
                        domestic=domestic, sources={key: source}, attempts=[])
        put_snapshot(files, domain, snapshot)
        files[domain + "/" + raw_name] = raw
    return delivery.encode(files)


def put_snapshot(files, domain, snapshot):
    prefix = domain + "/"
    base = prefix + "releases/" + snapshot["release_id"] + "/"
    raw = delivery.canonical(snapshot)
    manifest = delivery.canonical(dict(schema_version=1, release_id=snapshot["release_id"],
        snapshot_sha256=delivery.digest(raw), foreign_rows=len(snapshot["foreign"]), domestic_rows=len(snapshot["domestic"])))
    files[base + "snapshot.json"] = raw
    files[base + "manifest.json"] = manifest
    files[prefix + "current.json"] = delivery.canonical(dict(release_id=snapshot["release_id"], manifest_sha256=delivery.digest(manifest)))


def edit_snapshot(value, domain, edit):
    files = delivery.decode(value)
    pointer = delivery.strict_json(files[domain + "/current.json"])
    snapshot = delivery.strict_json(files[domain + "/releases/" + pointer["release_id"] + "/snapshot.json"])
    edit(snapshot)
    put_snapshot(files, domain, snapshot)
    return delivery.encode(files)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(delivery.canonical(value))
    return path


def export(root, value):
    files = delivery.decode(value)
    snapshots, _ = delivery.validate_archive(value, ROOT)
    manifest = {"schema_version": "commodity-positions-bundle/1", "domains": {}, "files": {}}
    for domain, snapshot in snapshots.items():
        manifest["domains"][domain] = {"release_id": snapshot["release_id"], "foreign_rows": len(snapshot["foreign"]), "domestic_rows": len(snapshot["domestic"])}
    for name, raw in files.items():
        path = root / "domains" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        manifest["files"]["domains/" + name] = delivery.digest(raw)
    return write(root / "bundle.json", manifest)


def delta(value, baseline=None):
    host = producer.delivery._host_contract()
    semantic = delivery.observations(value, baseline, ROOT)
    contract = host.DOMAIN_CONTRACTS[producer.DOMAIN]
    return {"schema_version": host.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "positions-fixture-001",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(), "domain": producer.DOMAIN,
            "producer": {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN},
            "run": {"run_id": "positions-fixture-001", "git_head": "a" * 40, "business_status": "initialized" if baseline is None else "updated" if semantic["business_changed"] else "no_change", "published": False},
            "baseline": {"files": dict.fromkeys(contract["baseline"])},
            "domain_metadata": {name: semantic[name] for name in contract["metadata"]},
            "payloads": {"positions_archive.json": {"sha256": "c" * 64, "size_bytes": 12}}}


def test_sources_replay_and_no_change_ignore_only_retrieval_time():
    sample = archive()
    assert delivery.observations(sample, None, ROOT)["record_count"] == 17
    changed = edit_snapshot(sample, "sugar", lambda s: s["foreign"][0].update(retrieved_at="2026-10-08T01:00:00Z"))
    assert not delivery.observations(changed, sample, ROOT)["business_changed"]
    revised = delivery.observations(archive(amount=21), sample, ROOT)
    assert len(revised["revised_partitions"]) == 3 and revised["business_changed"]


@pytest.mark.parametrize("mutation", ["value", "group_missing", "provenance", "wrong_domain", "future", "url", "missing_source"])
def test_rehashed_snapshots_still_require_complete_original_reports(mutation):
    def edit(s):
        if mutation == "value": s["foreign"][0]["long"] += 1
        elif mutation == "group_missing": s["foreign"].pop()
        elif mutation == "provenance": s["foreign"][0]["source_url"] = "https://example.invalid/report"
        elif mutation == "wrong_domain": s["foreign"][0]["market"] = "canola"
        elif mutation == "future": s["foreign"][0]["report_date"] = "2099-01-01"
        elif mutation == "url": next(iter(s["sources"].values()))["url"] = "https://example.invalid/report"
        else: s["sources"] = {}
    with pytest.raises(ValueError):
        delivery.validate_archive(edit_snapshot(archive(), "sugar", edit), ROOT)


@pytest.mark.parametrize("mutation", ["traversal", "backslash", "extra", "missing", "hash", "oversize", "base64", "boolean_size"])
def test_closed_archive_rejects_paths_sizes_hashes_and_unknown_files(mutation):
    value = archive()
    entry = next(iter(value["files"].values()))
    if mutation in {"traversal", "backslash", "extra"}:
        value["files"][{"traversal": "sugar/../outside", "backslash": "sugar\\outside", "extra": "sugar/script.py"}[mutation]] = deepcopy(entry)
    elif mutation == "missing": value["files"].pop("sugar/current.json")
    elif mutation == "hash": entry["sha256"] = "f" * 64
    elif mutation == "oversize": entry["size_bytes"] = delivery.MAX_FILE + 1
    elif mutation == "base64": entry["bytes_base64"] = "!" * len(entry["bytes_base64"])
    else: entry["size_bytes"] = True
    with pytest.raises((ValueError, KeyError)):
        delivery.validate_archive(value, ROOT)


def test_json_duplicate_keys_nonfinite_and_total_bounds(monkeypatch):
    for raw in (b'{"files":{},"files":{}}', b'{"value":NaN}', b'{"value":1e999}'):
        with pytest.raises(ValueError): delivery.strict_json(raw)
    sample = archive()
    monkeypatch.setattr(delivery, "MAX_TOTAL", 1)
    with pytest.raises(ValueError, match="total size"):
        delivery.decode(sample)


def test_history_partition_cannot_disappear_even_with_valid_new_source():
    with pytest.raises(ValueError, match="drops historical"):
        delivery.observations(archive(day="2026-09-30"), archive(), ROOT)


def test_materialization_preserves_immutable_history_and_verifies_actual_inputs(tmp_path):
    old = archive()
    delivery.materialize(old, tmp_path)
    old_files = delivery.decode(old)
    delivery.materialize(archive(amount=21), tmp_path)
    for name, raw in old_files.items():
        if "/releases/" in name or "/raw/" in name:
            assert (tmp_path / name).read_bytes() == raw
    delivery.verify_materialized(archive(amount=21), tmp_path)
    (tmp_path / "palm/current.json").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="materialized"):
        delivery.verify_materialized(archive(amount=21), tmp_path)
    immutable = tmp_path / "sugar/releases/20261008T000000Z_deadbeef/snapshot.json"
    immutable.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="immutable"):
        delivery.materialize(old, tmp_path)


def test_export_identity_rejects_changed_raw_and_extra_files(tmp_path):
    sample = archive()
    export(tmp_path, sample)
    assert delivery.archive_bundle(tmp_path, ROOT) == sample
    next(tmp_path.rglob("*.html")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="SHA"):
        delivery.archive_bundle(tmp_path, ROOT)


@pytest.mark.parametrize("mutation", [None, "extra", "partial", "parquet", "other_domain", "wrong_date"])
def test_host_and_json_schema_require_positions_only_contract(mutation):
    host = producer.delivery._host_contract()
    value = delta(archive())
    schema = Draft202012Validator(json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_text(encoding="utf-8")))
    if mutation is None:
        assert host.validate_delta_document(value) == value
        schema.validate(value)
        return
    if mutation == "extra": value["domain_metadata"]["schedule"] = "daily"
    elif mutation == "partial": value["baseline"]["files"][host.POSITIONS_STABLE] = {"sha256": "f" * 64, "size_bytes": 1}
    elif mutation == "parquet": value["payloads"]["positions_archive.json"]["row_count"] = 1
    elif mutation == "other_domain": value["baseline"]["files"][host.EXPORTS_STABLE] = None
    else: value["domain_metadata"]["latest_dates"]["sugar/foreign/sugar11/futures_only"] = "2026-02-30"
    with pytest.raises(ValueError): host.validate_delta_document(value)
    if mutation in {"extra", "parquet", "other_domain"}:
        with pytest.raises(ValidationError): schema.validate(value)


@pytest.fixture
def config(tmp_path):
    host = producer.delivery._host_contract()
    bundle = tmp_path / "export"
    manifest = export(bundle, archive())
    root = tmp_path / "baseline"
    baseline = write(root / "baseline_manifest.json", {"schema_version": producer.BASELINE_SCHEMA,
        "source_root": "/var/lib/market-data/production-runtime/test-positions", "files": dict.fromkeys(host.DOMAIN_CONTRACTS[producer.DOMAIN]["baseline"])})
    return {"schema_version": producer.CONFIG_SCHEMA, "approved_commit": "a" * 40, "approved_tree": "b" * 40,
            "origin": producer.delivery.ORIGIN, "python": str(Path(sys.executable).resolve()), "runtime_root": str(tmp_path / "runs"),
            "baseline_root": str(root), "baseline_manifest_sha256": producer.delivery.sha256_file(baseline),
            "bundle_root": str(bundle), "bundle_manifest_sha256": producer.delivery.sha256_file(manifest),
            "ssh_target": "tencent-market", "publisher": "/var/lib/market-data/producer/publisher.py", "publisher_sha256": "d" * 64,
            "image_id": "sha256:" + "e" * 64, "remote_allocation": "/var/lib/market-data/production-runtime/test-positions",
            "policy": {producer.DOMAIN: "/etc/market-data/production-data-delivery/positions.json"}, "policy_sha256": {producer.DOMAIN: "f" * 64}}


@pytest.fixture
def authorized(monkeypatch):
    monkeypatch.setattr(producer, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=True), dont_write_bytecode=True, executable=sys.executable))
    monkeypatch.setattr(producer.delivery, "verify_clean_detached_clone", lambda *_: {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN})


@pytest.mark.parametrize("publish", [False, True])
def test_producer_replays_bundle_without_network_and_only_publishes_explicitly(config, authorized, monkeypatch, publish):
    calls = []
    def send(cfg, package):
        with pytest.raises(Timeout):
            with FileLock(str(Path(cfg["runtime_root"]) / "commodity-positions-delivery.lock"), timeout=0): pass
        assert producer.delivery._host_contract().validate_delta_document(delivery.read_json(package / "delta_contract.json"))
        calls.append(package)
        return {"status": "PUBLISHED"}
    monkeypatch.setattr(producer.delivery, "invoke_publisher", send)
    result = producer.run_positions(config, publish=publish)
    assert result["status"] == ("PUBLISHED" if publish else "CANDIDATE")
    assert bool(calls) == publish and not result["collection_performed"]


@pytest.mark.parametrize("failure", ["pin", "raw", "baseline", "overlap", "scope"])
def test_invalid_input_never_invokes_publish(config, authorized, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(producer.delivery, "invoke_publisher", lambda *_: calls.append(True))
    if failure == "pin": config["bundle_manifest_sha256"] = "0" * 64
    elif failure == "raw": next(Path(config["bundle_root"]).rglob("*.html")).write_bytes(b"corrupt")
    elif failure == "baseline": write(Path(config["baseline_root"]) / "extra.json", {})
    elif failure == "overlap": config["runtime_root"] = config["bundle_root"]
    else: config["policy"]["canola_exports"] = config["policy"][producer.DOMAIN]
    with pytest.raises(ValueError): producer.run_positions(config, publish=True)
    assert not calls


@pytest.mark.parametrize("mode", ["initialize", "update", "no_change", "status_failure", "drift", "materialized_drift"])
def test_publication_is_atomic_retains_history_and_protects_canada(tmp_path, monkeypatch, mode):
    host = producer.delivery._host_contract()
    allocation, candidate = tmp_path / "allocation", tmp_path / "package"
    contract = host.DOMAIN_CONTRACTS[producer.DOMAIN]
    formal = allocation / contract["domain_dir"]
    old = None if mode == "initialize" else archive()
    sample = archive(amount=21) if mode not in {"initialize", "no_change"} else archive()
    write(allocation / host.EXPORTS_STABLE, {"canada": "untouched"})
    if old is not None:
        write(allocation / host.POSITIONS_STABLE, old)
        write(allocation / host.POSITIONS_STATUS, {"old": True})
        delivery.materialize(old, formal)
    semantic = delivery.observations(sample, old, ROOT)
    value = delta(sample, old)
    value["baseline"]["files"] = {name: host._actual_identity(allocation / name) for name in contract["baseline"]}
    value["payloads"]["positions_archive.json"] = host._actual_identity(write(candidate / "positions_archive.json", sample))
    write(candidate / host.MANIFEST_NAME, value)
    report = write(tmp_path / "report.json", {})
    policy = {"policy_id": "positions-policy", "domain": producer.DOMAIN, "allocation_root": str(allocation), "approved_producer": value["producer"]}
    monkeypatch.setattr(host, "_load_policy", lambda _: (policy, "f" * 64))
    monkeypatch.setattr(host, "_protected", lambda p, **_: Path(p))
    monkeypatch.setattr(host, "_protected_tree", lambda p: Path(p))
    monkeypatch.setattr(host, "_under", lambda p, _root, **_: Path(p))
    monkeypatch.setattr(host, "_derived", lambda root, _policy, _id, suffix="": candidate if root == host.CANDIDATE_ROOT else tmp_path / ("backup" if root == host.BACKUP_ROOT else "evidence" + suffix))
    monkeypatch.setattr(host, "_load_pass_report", lambda *_: {"candidate_files": host._candidate_files(candidate, value), "semantic": {"observations": semantic}})
    monkeypatch.setattr(host, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(host, "_git_identity", lambda _: value["producer"])
    monkeypatch.setattr(host, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(host.os, "chown", lambda *_: None, raising=False)
    def exchange(left, right, flag):
        if flag == 1: left.rename(right)
        else:
            temp = left.with_name(left.name + ".swap")
            left.rename(temp); right.rename(left); temp.rename(right)
    monkeypatch.setattr(host, "_renameat2", exchange)
    if mode == "status_failure":
        def status_error(path, val):
            write(path, val)
            raise OSError("status fsync failure")
        monkeypatch.setattr(host, "_atomic_json", status_error)
    if mode == "drift": write(allocation / host.POSITIONS_STATUS, {"concurrent": True})
    if mode == "materialized_drift": (formal / "palm/current.json").write_bytes(b"corrupt")
    before = {str(p): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    if mode in {"status_failure", "drift", "materialized_drift"}:
        with pytest.raises((ValueError, OSError)):
            host.publish(tmp_path / "policy", value["delta_id"], report, host.sha256_file(report))
        for path, raw in before.items():
            assert Path(path).read_bytes() == raw
        assert {str(p) for p in formal.rglob("*") if p.is_file()} == {
            path for path in before if Path(path).is_relative_to(formal)}
    else:
        result = host.publish(tmp_path / "policy", value["delta_id"], report, host.sha256_file(report))
        assert result["status"] == ("NO_CHANGE" if mode == "no_change" else "PUBLISHED")
        delivery.verify_materialized(sample, formal)
        if mode == "no_change": assert before == {str(p): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
        if old is not None:
            for name, raw in delivery.decode(old).items():
                if "/releases/" in name or "/raw/" in name: assert (formal / name).read_bytes() == raw
    assert delivery.read_json(allocation / host.EXPORTS_STABLE) == {"canada": "untouched"}


def test_formal_cli_routes_positions_and_rejects_other_domain_options(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("positions_formal_cli", ROOT / "04_scripts/automation/run_production_data_delta_windows.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    config_path = write(tmp_path / "config.json", {})
    monkeypatch.setattr(cli, "_bootstrap", lambda _: None)
    monkeypatch.setattr(cli, "load_module", lambda: SimpleNamespace(verify_clean_detached_clone=lambda *_: None))
    calls = []
    monkeypatch.setattr(producer, "run_positions", lambda cfg, publish=False: calls.append(publish) or {"status": "CANDIDATE"})
    assert cli.main(["--config", str(config_path), "--domain", producer.DOMAIN]) == 0
    assert calls == [False]
    assert cli.main(["--config", str(config_path), "--domain", producer.DOMAIN, "--end-date", "2026-10-08"]) == 1
    assert calls == [False]


def test_worker_replays_actual_inputs_and_rejects_wrong_image_metadata_and_unmanaged_baseline(tmp_path, monkeypatch):
    host = producer.delivery._host_contract()
    candidate, allocation = tmp_path / "candidate", tmp_path / "allocation"
    sample = archive()
    value = delta(sample)
    value["payloads"]["positions_archive.json"] = host._actual_identity(write(candidate / "positions_archive.json", sample))
    write(candidate / host.MANIFEST_NAME, value)
    original_read = host.read_json
    monkeypatch.setattr(host, "Path", lambda path: candidate if path == "/candidate" else allocation if path == "/allocation" else ROOT if path == "/app" else Path(path))
    monkeypatch.setattr(host, "read_json", lambda path: {"git_commit": "a" * 40, "git_tree": "b" * 40} if str(path) == "/app/RELEASE.json" else original_read(path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", "b" * 40)
    assert host.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)["status"] == "PASS"
    with pytest.raises(ValueError, match="RELEASE identity"):
        host.worker_validate(producer.DOMAIN, "c" * 40, "b" * 40)
    value["domain_metadata"]["record_count"] += 1
    write(candidate / host.MANIFEST_NAME, value)
    with pytest.raises(ValueError, match="metadata mismatch"):
        host.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)
    value["domain_metadata"]["record_count"] -= 1
    write(candidate / host.MANIFEST_NAME, value)
    write(allocation / host.DOMAIN_CONTRACTS[producer.DOMAIN]["domain_dir"] / "sugar/current.json", {"unmanaged": True})
    with pytest.raises(ValueError, match="unmanaged positions"):
        host.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)


def test_host_codec_is_stdlib_only_and_compatible_with_python_310(tmp_path):
    path = ROOT / "03_src/agri_research_agent/positions/delivery.py"
    code = '''
import ast,importlib.util,sys
from pathlib import Path
path=Path(sys.argv[1]); ast.parse(path.read_text(encoding="utf-8"),feature_version=(3,10))
spec=importlib.util.spec_from_file_location("host_positions_codec",path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert not any(k in sys.modules for k in ["agri_research_agent","openpyxl","bs4","pandas","requests"])
sample=module.encode({"sugar/current.json":b"host-codec-fixture"})
module.materialize(sample,Path(sys.argv[2]));module.verify_materialized(sample,Path(sys.argv[2]))
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", code, str(path), str(tmp_path)],
                            capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8")

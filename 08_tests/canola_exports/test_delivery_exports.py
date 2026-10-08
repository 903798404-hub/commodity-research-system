from __future__ import annotations

import base64
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import gzip
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from filelock import FileLock, Timeout
from jsonschema import Draft202012Validator, ValidationError
import pytest

from agri_research_agent.automation import production_data_delta_exports as producer
from agri_research_agent.canola_exports import data, delivery, update
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
from test_exports import bundle, excel_report, source

ROOT = Path(__file__).resolve().parents[2]


def host():
    return producer.delivery._host_contract()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(producer.delivery.canonical_json_bytes(value))
    return path


def evidence(year="2026-2027", raw=None):
    raw = raw if raw is not None else source(year)
    return {"schema_version": delivery.EVIDENCE_SCHEMA, "sources": [{
        "kind": "csv", "crop_year": year, "sha256": data.digest(raw), "source_url": data.source_url(year),
        "encoding": "gzip", "bytes_base64": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")}]}


def delta(module, sample=None, sources=None):
    sample = sample if sample is not None else bundle(("2026-2027", source()))
    contract = module.DOMAIN_CONTRACTS[producer.DOMAIN]
    return {"schema_version": module.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "export-delta-001",
            "generated_at_utc": update.now(), "domain": producer.DOMAIN,
            "producer": {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN},
            "run": {"run_id": "export-delta-001", "git_head": "a" * 40, "business_status": "initialized", "published": False},
            "baseline": {"files": dict.fromkeys(contract["baseline"])},
            "domain_metadata": {name: delivery.observations(sample, None, sources or evidence())[name]
                                for name in contract["metadata"]},
            "payloads": {name: {"sha256": "c" * 64, "size_bytes": 12} for name in contract["payloads"]}}


@pytest.mark.parametrize("mutation", [None, "extra", "partial", "parquet", "wrong_year", "wrong_date", "crop_path"])
def test_contract_schema_is_closed_and_separate_from_crop_progress(mutation):
    module = host()
    value = delta(module)
    schema = Draft202012Validator(json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_text(encoding="utf-8")))
    if mutation is None:
        assert module.validate_delta_document(value) == value
        schema.validate(value)
        assert not set(module.DOMAIN_CONTRACTS[producer.DOMAIN]["baseline"]) & set(module.DOMAIN_CONTRACTS["canada_canola"]["baseline"])
        return
    if mutation == "extra":
        value["domain_metadata"]["schedule"] = "weekly"
    elif mutation == "partial":
        value["baseline"]["files"][module.EXPORTS_STABLE] = {"sha256": "c" * 64, "size_bytes": 12}
    elif mutation == "parquet":
        value["payloads"]["weekly.json"]["row_count"] = 12
    elif mutation == "wrong_year":
        value["domain_metadata"]["crop_years"] = ["2026-2028"]
    elif mutation == "wrong_date":
        value["domain_metadata"]["latest_cutoff"] = "2026-02-30"
    else:
        value["baseline"]["files"][module.CANOLA_STABLE] = None
    with pytest.raises(ValueError):
        module.validate_delta_document(value)
    if mutation in {"extra", "parquet", "crop_path"}:
        with pytest.raises(ValidationError):
            schema.validate(value)


@pytest.mark.parametrize("mutation", ["value", "missing_week", "raw_bytes", "url", "duplicate_source", "extra_source", "extra_field", "revision"])
def test_offline_replay_rejects_tampered_data_and_sources(mutation):
    baseline = bundle(("2026-2027", source()))
    sample, sources = deepcopy(baseline), evidence()
    if mutation == "value":
        sample["records"][0]["weekly_mt"] = 0
    elif mutation == "missing_week":
        sample["records"].pop()
    elif mutation == "raw_bytes":
        sources["sources"][0]["bytes_base64"] = base64.b64encode(b"wrong").decode()
    elif mutation == "url":
        sources["sources"][0]["source_url"] = "https://example.invalid/file.csv"
    elif mutation == "duplicate_source":
        sources["sources"].append(deepcopy(sources["sources"][0]))
    elif mutation == "extra_source":
        sources["sources"].extend(evidence("2025-2026")["sources"])
    elif mutation == "extra_field":
        sample["records"][0]["sales"] = 999
    else:
        sample["revisions"].append({"crop_year": "2026-2027", "grain_week": 1, "metric": "weekly_mt",
                                    "old_value": 0, "new_value": 7000, "source_sha256": "c" * 64, "observed_at": update.now()})
    with pytest.raises(ValueError):
        delivery.observations(sample, baseline, sources)


def test_evidence_bounds_are_enforced_before_base64_decode(monkeypatch):
    sources = evidence()
    monkeypatch.setattr(delivery, "MAX_PACKED_SOURCE", 1)
    with pytest.raises(ValueError, match="too large"):
        delivery.decode_evidence(sources)


@pytest.mark.parametrize("bound", ["MAX_SOURCE", "MAX_TOTAL", "MAX_PACKED_TOTAL"])
def test_compressed_evidence_cannot_exceed_decompression_or_total_bounds(monkeypatch, bound):
    sources = evidence()
    monkeypatch.setattr(delivery, bound, 1)
    with pytest.raises(ValueError, match="size bound"):
        delivery.decode_evidence(sources)


def test_evidence_hash_is_of_official_bytes_not_compressed_transfer():
    sources = evidence()
    row = sources["sources"][0]
    raw = source()
    decoded = delivery.decode_evidence(sources)
    assert list(decoded.values()) == [raw]
    assert row["sha256"] == data.digest(raw)
    assert row["sha256"] != data.digest(base64.b64decode(row["bytes_base64"]))


def test_source_backed_revisions_and_no_change_ignore_download_timestamps(tmp_path):
    root = tmp_path / "owned"
    write(root / ".market-data-runtime.json", {"schema_version": 1, "runtime_id": "export-fixture",
          "classification": "isolated-dev", "module_id": data.MODULE_ID, "created_at": update.now()})
    context = RuntimeContext(RuntimeMode.ISOLATED_DEV, data.MODULE_ID, root)
    first = update.prepare(context, {"2026-2027": source()})
    update.activate_local(context, first)
    baseline = data.load_bundle(root / data.STABLE)
    second = update.prepare(context, {"2026-2027": source(weekly="2.0")})
    sample = data.load_bundle(second)
    result = delivery.observations(sample, baseline, delivery.archive_evidence(sample, root))
    assert result["revised"] == 5 and result["business_changed"]
    sample["revisions"].pop()
    with pytest.raises(ValueError, match="revision evidence"):
        delivery.observations(sample, baseline, delivery.archive_evidence(sample, root))
    unchanged = deepcopy(baseline)
    unchanged["sources"]["2026-2027"]["retrieved_at"] = update.now()
    assert not delivery.observations(unchanged, baseline, evidence())["business_changed"]


def test_excel_missing_value_replay_keeps_real_zero_and_requires_report_bytes():
    raw = source().replace(b",Vancouver,1.0", b",Vancouver,-")
    sample = bundle(("2026-2027", raw))
    report = excel_report(week=3, weekly=0)
    url = data.source_url("2026-2027").rsplit("/", 1)[0] + "/03-grain-stats-weekly-2026-2027.xlsx"
    data.reconcile_report(sample["records"], "2026-2027", 3, url, report)
    sources = evidence(raw=raw)
    with pytest.raises(ValueError, match="Excel evidence missing"):
        delivery.replay(sample, sources)
    sources["sources"].append({"kind": "xlsx", "crop_year": "2026-2027", "sha256": data.digest(report),
                                "source_url": url, "encoding": "gzip",
                                "bytes_base64": base64.b64encode(gzip.compress(report, mtime=0)).decode()})
    delivery.replay(sample, sources)
    assert sample["records"][2]["weekly_mt"] == 0
    assert sample["records"][0]["weekly_mt"] is None


@pytest.fixture
def config(tmp_path):
    module = host()
    root = tmp_path / "baseline"
    manifest = {"schema_version": producer.BASELINE_SCHEMA,
                "source_root": "/var/lib/market-data/production-runtime/test-exports",
                "files": dict.fromkeys(module.DOMAIN_CONTRACTS[producer.DOMAIN]["baseline"])}
    path = write(root / "baseline_manifest.json", manifest)
    return {"schema_version": producer.CONFIG_SCHEMA, "approved_commit": "a" * 40, "approved_tree": "b" * 40,
            "origin": producer.delivery.ORIGIN, "python": str(Path(sys.executable).resolve()),
            "runtime_root": str(tmp_path / "runs"), "baseline_root": str(root),
            "baseline_manifest_sha256": module.sha256_file(path), "ssh_target": "tencent-market",
            "publisher": "/var/lib/market-data/production-input-producers/approved/09_deploy/production_data_delivery/activate_production_data_delta.py",
            "publisher_sha256": "d" * 64, "image_id": "sha256:" + "e" * 64,
            "remote_allocation": manifest["source_root"],
            "policy": {producer.DOMAIN: "/etc/market-data/production-data-delivery/exports.json"},
            "policy_sha256": {producer.DOMAIN: "f" * 64}, "initial_years": 7, "expected_cutoff": None}


@pytest.mark.parametrize("mutation", [None, "extra", "image_tag", "origin", "scope", "years", "cutoff", "relative"])
def test_manual_config_pins_identity_and_has_no_scheduler_switch(config, mutation):
    if mutation is None:
        assert producer.validate_config(config) == config
        return
    if mutation == "extra":
        config["schedule"] = "weekly"
    elif mutation == "image_tag":
        config["image_id"] = "latest"
    elif mutation == "origin":
        config["origin"] = "file:///preview"
    elif mutation == "scope":
        config["policy"]["canada_canola"] = config["policy"][producer.DOMAIN]
    elif mutation == "years":
        config["initial_years"] = True
    elif mutation == "cutoff":
        config["expected_cutoff"] = "2026-02-30"
    else:
        config["runtime_root"] = "runs"
    with pytest.raises(ValueError):
        producer.validate_config(config)


def install_baseline(config, sample=None):
    module = host()
    sample = sample or bundle(("2026-2027", source()))
    root = Path(config["baseline_root"])
    values = {module.EXPORTS_STABLE: sample, module.EXPORTS_SOURCES: evidence(), module.EXPORTS_STATUS: {"published": True}}
    files = {relative: producer.delivery._identity(write(root / relative, content)) for relative, content in values.items()}
    path = write(root / "baseline_manifest.json", {"schema_version": producer.BASELINE_SCHEMA,
                 "source_root": config["remote_allocation"], "files": files})
    config["baseline_manifest_sha256"] = module.sha256_file(path)
    return sample


def test_baseline_is_pinned_complete_and_rejects_extra_files(config):
    assert all(v is None for v in producer.verify_baseline(config).values())
    baseline = install_baseline(config)
    assert producer.verify_baseline(config)[host().EXPORTS_STABLE] is not None
    write(Path(config["baseline_root"]) / host().EXPORTS_STABLE, {**baseline, "generated_at": "changed"})
    with pytest.raises(ValueError, match="identity differs"):
        producer.verify_baseline(config)


@pytest.mark.parametrize("existing", [False, True])
def test_collector_restores_only_owned_runtime_and_uses_bounded_prepare(config, tmp_path, monkeypatch, existing):
    baseline = install_baseline(config) if existing else None
    work = tmp_path / "owned-job"
    work.mkdir()
    pinned_before = {str(p): p.read_bytes() for p in Path(config["baseline_root"]).rglob("*") if p.is_file()}
    calls = []

    def collect(argv, **kwargs):
        calls.append((argv, kwargs))
        root = Path(argv[argv.index("--runtime-root") + 1])
        context = RuntimeContext(RuntimeMode.ISOLATED_DEV, data.MODULE_ID, root)
        path = update.prepare(context, {"2026-2027": source()})
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"status": "PASS", "path": str(path)}))

    monkeypatch.setattr(producer.delivery, "_run", collect)
    candidate, sources = producer._collect(config, work, baseline, evidence() if existing else None)
    assert candidate.is_relative_to(work)
    delivery.replay(data.load_bundle(candidate), sources)
    assert calls[0][0][-1] == ("2" if existing else "7")
    assert "-I" in calls[0][0] and "-B" in calls[0][0] and "prepare" in calls[0][0]
    assert calls[0][1]["timeout"] == 1200
    assert pinned_before == {str(p): p.read_bytes() for p in Path(config["baseline_root"]).rglob("*") if p.is_file()}


@pytest.fixture
def authorized_producer(monkeypatch):
    monkeypatch.setattr(producer, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=True), dont_write_bytecode=True, executable=sys.executable))
    monkeypatch.setattr(producer.delivery, "verify_clean_detached_clone", lambda *_: {
        "commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN})


@pytest.mark.parametrize("existing,publish", [(False, False), (False, True), (True, False), (True, True)])
def test_manual_run_builds_candidate_and_publishes_only_explicitly(config, monkeypatch, authorized_producer, existing, publish):
    if existing:
        install_baseline(config)
    calls = []

    def collect(_config, work, baseline, _evidence):
        with pytest.raises(Timeout):
            with FileLock(str(Path(config["runtime_root"]) / "canola-exports-delivery.lock"), timeout=0):
                pass
        path = write(work / "fixture.json", baseline or bundle(("2026-2027", source())))
        return path, evidence()

    def publish_candidate(_config, package):
        calls.append(package)
        value = json.loads((package / "delta_contract.json").read_text())
        assert value["run"]["business_status"] == ("no_change" if existing else "initialized")
        return {"status": "NO_CHANGE" if existing else "PUBLISHED"}

    monkeypatch.setattr(producer, "_collect", collect)
    monkeypatch.setattr(producer.delivery, "invoke_publisher", publish_candidate)
    result = producer.run_exports(config, publish=publish)
    expected = ("NO_CHANGE" if existing else "PUBLISHED") if publish else "CANDIDATE"
    assert result["status"] == expected
    assert bool(calls) == publish and result["trigger"] == "manual"
    assert result["published"] == (publish and not existing)
    assert "latest_cutoff" in result["observations"]


@pytest.mark.parametrize("failure", ["download", "expected_cutoff", "baseline_drift", "lock", "overlap"])
def test_failed_manual_run_never_invokes_publication(config, monkeypatch, authorized_producer, failure):
    install_baseline(config)
    snapshot = {str(p): p.read_bytes() for p in Path(config["baseline_root"]).rglob("*") if p.is_file()}
    calls = []
    monkeypatch.setattr(producer.delivery, "invoke_publisher", lambda *_: calls.append(True))

    def collect(_config, work, baseline, _evidence):
        if failure == "download":
            raise producer.delivery.ProductionDataError("bounded child command failed")
        if failure == "baseline_drift":
            write(Path(config["baseline_root"]) / "extra.json", {})
        return write(work / "fixture.json", baseline), evidence()

    monkeypatch.setattr(producer, "_collect", collect)
    if failure == "expected_cutoff":
        config["expected_cutoff"] = "2026-09-27"
    if failure == "overlap":
        config["runtime_root"] = config["baseline_root"]
    lock = FileLock(str(Path(config["runtime_root"]) / "canola-exports-delivery.lock"), timeout=0)
    if failure == "lock":
        lock.acquire()
    try:
        with pytest.raises(ValueError):
            producer.run_exports(config, publish=True)
    finally:
        if failure == "lock":
            lock.release()
    assert not calls
    for path, raw in snapshot.items():
        assert Path(path).read_bytes() == raw
    results = list(Path(config["runtime_root"]).glob("*/result.json"))
    if failure not in {"lock", "overlap"}:
        assert len(results) == 1 and json.loads(results[0].read_text())["status"] == "FAIL"


def test_worker_replays_sources_and_rejects_wrong_metadata_image_and_missing_evidence(tmp_path, monkeypatch):
    module = host()
    candidate = tmp_path / "candidate"
    sample, sources = bundle(("2026-2027", source())), evidence()
    value = delta(module, sample, sources)
    for name, content in {"weekly.json": sample, "source_evidence.json": sources}.items():
        path = write(candidate / name, content)
        value["payloads"][name] = module._actual_identity(path)
    write(candidate / module.MANIFEST_NAME, value)
    original_read = module.read_json
    monkeypatch.setattr(module, "Path", lambda path: candidate if path == "/candidate" else tmp_path / "allocation" if path == "/allocation" else Path(path))
    monkeypatch.setattr(module, "read_json", lambda path: {"git_commit": "a" * 40, "git_tree": "b" * 40} if str(path) == "/app/RELEASE.json" else original_read(path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", "b" * 40)
    result = module.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)
    assert result["status"] == "PASS" and result["observations"]["added"] == 5
    with pytest.raises(ValueError, match="RELEASE identity"):
        module.worker_validate(producer.DOMAIN, "c" * 40, "b" * 40)
    value["domain_metadata"]["record_count"] += 1
    write(candidate / module.MANIFEST_NAME, value)
    with pytest.raises(ValueError, match="metadata mismatch"):
        module.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)
    value["domain_metadata"]["record_count"] -= 1
    write(candidate / module.MANIFEST_NAME, value)
    write(candidate / "source_evidence.json", {"schema_version": delivery.EVIDENCE_SCHEMA, "sources": []})
    with pytest.raises(ValueError, match="CSV evidence missing"):
        module.worker_validate(producer.DOMAIN, "a" * 40, "b" * 40)


@pytest.mark.parametrize("mode", ["initialize", "update", "no_change", "status_failure", "drift"])
def test_host_publication_keeps_other_domains_and_restores_failed_updates(tmp_path, monkeypatch, mode):
    module = host()
    allocation, candidate = tmp_path / "allocation", tmp_path / "package"
    contract = module.DOMAIN_CONTRACTS[producer.DOMAIN]
    before = bundle(("2026-2027", source()))
    formal = allocation / contract["domain_dir"]
    write(allocation / module.CANOLA_STABLE, {"crop": "untouched"})
    if mode != "initialize":
        for relative, content in {module.EXPORTS_STABLE: before, module.EXPORTS_SOURCES: evidence(), module.EXPORTS_STATUS: {"old": True}}.items():
            write(allocation / relative, content)
        (formal / "notes.txt").write_bytes(b"retain")
    sample = deepcopy(before)
    if mode not in {"initialize", "no_change"}:
        raw = source(weeks=(1, 2, 3, 4, 5, 6))
        sample = bundle(("2026-2027", raw))
        sources = evidence(raw=raw)
    else:
        sources = evidence()
    semantic = delivery.observations(sample, None if mode == "initialize" else before, sources)
    value = delta(module, sample, sources)
    value["baseline"]["files"] = {relative: module._actual_identity(allocation / relative) for relative in contract["baseline"]}
    value["run"]["business_status"] = "initialized" if mode == "initialize" else "no_change" if mode == "no_change" else "updated"
    for name, content in {"weekly.json": sample, "source_evidence.json": sources}.items():
        value["payloads"][name] = module._actual_identity(write(candidate / name, content))
    write(candidate / module.MANIFEST_NAME, value)
    report_path = write(tmp_path / "report.json", {})
    policy = {"policy_id": "export-policy", "domain": producer.DOMAIN, "allocation_root": str(allocation), "approved_producer": value["producer"]}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, _root, **_: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, _policy, _id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("backup" if root == module.BACKUP_ROOT else "evidence" + suffix))
    monkeypatch.setattr(module, "_load_pass_report", lambda *_: {"candidate_files": module._candidate_files(candidate, value), "semantic": {"observations": semantic}})
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_git_identity", lambda _: value["producer"])
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(module.os, "chown", lambda *_: None, raising=False)

    def exchange(left, right, flag):
        if flag == 1:
            left.rename(right)
        else:
            temporary = left.with_name(left.name + ".swap")
            left.rename(temporary)
            right.rename(left)
            temporary.rename(right)

    monkeypatch.setattr(module, "_renameat2", exchange)
    if mode == "status_failure":
        def status_error(path, value):
            write(path, value)
            raise OSError("status fsync failure")
        monkeypatch.setattr(module, "_atomic_json", status_error)
    if mode == "drift":
        write(allocation / module.EXPORTS_STATUS, {"concurrent": True})
    snapshot = {str(p): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
    if mode in {"status_failure", "drift"}:
        with pytest.raises((ValueError, OSError)):
            module.publish(tmp_path / "policy", value["delta_id"], report_path, module.sha256_file(report_path))
        for path, raw in snapshot.items():
            assert Path(path).read_bytes() == raw
        assert {p.name for p in formal.iterdir()} == {"weekly.json", "source_evidence.json", "notes.txt"}
    else:
        result = module.publish(tmp_path / "policy", value["delta_id"], report_path, module.sha256_file(report_path))
        assert result["status"] == ("NO_CHANGE" if mode == "no_change" else "PUBLISHED")
        if mode == "no_change":
            assert snapshot == {str(p): p.read_bytes() for p in allocation.rglob("*") if p.is_file()}
        else:
            status = json.loads((allocation / module.EXPORTS_STATUS).read_text())
            assert status["latest_cutoff"] == semantic["latest_cutoff"] and status["published"]
            assert data.load_bundle(allocation / module.EXPORTS_STABLE) == sample
    assert json.loads((allocation / module.CANOLA_STABLE).read_text()) == {"crop": "untouched"}
    if mode != "initialize":
        assert (formal / "notes.txt").read_bytes() == b"retain"


def test_formal_entrypoint_routes_manual_exports_and_rejects_unrelated_options(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("export_formal_cli_test", ROOT / "04_scripts/automation/run_production_data_delta_windows.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = write(tmp_path / "config.json", {})
    monkeypatch.setattr(cli, "_bootstrap", lambda _: None)
    monkeypatch.setattr(cli, "load_module", lambda: SimpleNamespace(verify_clean_detached_clone=lambda *_: None))
    calls = []
    monkeypatch.setattr(producer, "run_exports", lambda config, publish=False: calls.append(publish) or {"status": "CANDIDATE"})
    assert cli.main(["--config", str(path), "--domain", producer.DOMAIN]) == 0
    assert calls == [False]
    assert cli.main(["--config", str(path), "--domain", producer.DOMAIN, "--publish"]) == 0
    assert calls == [False, True]
    assert cli.main(["--config", str(path), "--domain", producer.DOMAIN, "--end-date", "2026-09-27"]) == 1
    assert calls == [False, True]

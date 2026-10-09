from contextlib import nullcontext
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest

from agri_research_agent.automation import production_data_delta_fx as producer
from agri_research_agent.market_data.foreign_fx import load_update_status

ROOT = Path(__file__).resolve().parents[1]


def host():
    spec = importlib.util.spec_from_file_location("fx_host_test", ROOT / "09_deploy/production_data_delivery/activate_production_data_delta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state,needed", [(None, True), ({"status": "FAIL"}, True), ({"status": "CANDIDATE"}, True),
    ({"status": "PUBLISHED"}, False), ({"status": "NO_CHANGE"}, False)])
def test_retry_windows_only_run_without_same_day_successful_delivery(state, needed):
    identity = {"commit": "a" * 40, "tree": "b" * 40}
    now = datetime(2024, 1, 10, 23, 30, tzinfo=timezone.utc)  # Jan 11 07:30 Beijing.
    if state is not None:
        state = {**state, "producer": identity, "checked_at": "2024-01-10T23:00:00+00:00"}
    assert producer.retry_needed(state, identity, now) is needed
    if state is not None:
        assert producer.retry_needed(state, {"commit": "c" * 40}, now)
        assert producer.retry_needed(state, identity, datetime(2024, 1, 11, 23, tzinfo=timezone.utc))


def test_protected_snapshot_exports_only_fixed_domain_bytes_and_rejects_partial_state(monkeypatch, tmp_path):
    module = host()
    allocation = tmp_path / "allocation"
    allocation.mkdir()
    policy = {"domain": "foreign_fx", "allocation_root": str(allocation), "approved_producer": {"commit": "a" * 40},
              "validation_image": {"image_id": "sha256:" + "b" * 64}}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "c" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    cold = module.snapshot_fx_baseline("policy")
    assert set(cold["files"]) == set(module.DOMAIN_CONTRACTS["foreign_fx"]["baseline"])
    assert all(value is None for value in cold["contents"].values())
    stable = allocation / module.FX_STABLE
    stable.parent.mkdir(parents=True)
    stable.write_bytes(b"{}")
    with pytest.raises(module.DeltaError, match="incomplete"):
        module.snapshot_fx_baseline("policy")
    (allocation / module.FX_SOURCES).write_bytes(b"{}")
    (allocation / module.FX_STATUS).write_bytes(b"{}")
    complete = module.snapshot_fx_baseline("policy")
    assert all(value is not None for value in complete["files"].values())
    (stable.parent / "unmanaged.json").write_bytes(b"{}")
    with pytest.raises(module.DeltaError, match="unmanaged"):
        module.snapshot_fx_baseline("policy")
    policy["domain"] = "commodity_positions"
    with pytest.raises(module.DeltaError, match="restricted"):
        module.snapshot_fx_baseline("policy")


def test_production_status_is_bound_to_actual_stable_sha_and_page_reads_it(tmp_path):
    module = host()
    stable = tmp_path / "daily.json"
    stable.write_bytes(b"{}")
    import hashlib, json
    identities = {module.FX_STABLE: {"sha256": hashlib.sha256(stable.read_bytes()).hexdigest()}}
    delta = {"domain_metadata": {}, "run": {"run_id": "fx-test", "business_status": "no_change"}}
    status = module._status_document("foreign_fx", delta, identities, "2024-01-11T00:00:00+00:00", False,
                                    {"latest_dates": {"BRL": "2024-01-10"}, "added": 0, "revised": 0})
    stable.with_name("status.json").write_text(json.dumps(status), encoding="utf-8")
    assert load_update_status(stable)["result"] == "NO_CHANGE"
    stable.write_bytes(b'{"changed":true}')
    assert load_update_status(stable) is None


def test_task_windows_are_explicit_beijing_and_use_hidden_launcher_for_approved_config():
    import xml.etree.ElementTree as ET
    from datetime import date
    xml = producer.task_xml(control_root=r"C:\control clone", config_path=r"C:\private config\汇率.json",
                            user_id=r"PC\xx202", start_date=date(2026, 10, 9))
    root = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    boundaries = [node.text for node in root.findall(".//t:StartBoundary", ns)]
    assert boundaries == ["2026-10-09T07:00:00+08:00", "2026-10-09T07:30:00+08:00", "2026-10-09T08:00:00+08:00"]
    arguments = root.find(".//t:Arguments", ns).text
    assert "-WindowStyle Hidden" in arguments and "汇率.json" in arguments and "launch_scheduled.ps1" in arguments
    assert root.find(".//t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert root.find(".//t:LogonType", ns).text == "InteractiveToken"


def test_failed_check_changes_only_status_and_cannot_overwrite_a_newly_published_snapshot(monkeypatch, tmp_path):
    import hashlib, json
    module = host()
    allocation = tmp_path / "allocation"
    stable = allocation / module.FX_STABLE
    stable.parent.mkdir(parents=True)
    raw = b'{"original":"quote"}'
    stable.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    status_path = allocation / module.FX_STATUS
    status_path.write_text(json.dumps({"schema_version": "foreign-fx-update/1", "stable_sha256": sha, "result": "UPDATED"}), encoding="utf-8")
    policy = {"domain": "foreign_fx", "allocation_root": str(allocation)}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "b" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    recorded = module.record_fx_failure("policy", "foreign-fx-test", sha, "FxDataError")
    assert recorded["status"] == "RECORDED" and stable.read_bytes() == raw
    assert json.loads(status_path.read_text())["result"] == "FAILED"
    before = status_path.read_bytes()
    stable.write_bytes(b'{"new":"quote"}')
    with pytest.raises(module.DeltaError, match="changed since"):
        module.record_fx_failure("policy", "foreign-fx-test", sha, "FxDataError")
    assert status_path.read_bytes() == before


def test_failed_status_fsync_restores_previous_status_and_keeps_quotes(monkeypatch, tmp_path):
    import hashlib, json
    module = host()
    allocation = tmp_path / "allocation"
    stable = allocation / module.FX_STABLE
    stable.parent.mkdir(parents=True)
    stable.write_bytes(b"old-quotes")
    sha = hashlib.sha256(stable.read_bytes()).hexdigest()
    status = allocation / module.FX_STATUS
    old = json.dumps({"schema_version": "foreign-fx-update/1", "stable_sha256": sha, "result": "UPDATED"}).encode()
    status.write_bytes(old)
    monkeypatch.setattr(module, "_load_policy", lambda _: ({"domain": "foreign_fx", "allocation_root": str(allocation)}, "a" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    def fail_after_write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        raise OSError("fsync failed after replacement")
    monkeypatch.setattr(module, "_atomic_json", fail_after_write)
    with pytest.raises(OSError, match="fsync"):
        module.record_fx_failure("policy", "foreign-fx-test", sha, "FxDataError")
    assert status.read_bytes() == old and stable.read_bytes() == b"old-quotes"


def test_each_protected_delivery_uses_fresh_host_baseline_and_checks_transferred_bytes(monkeypatch, tmp_path):
    import base64, hashlib, json
    module = host()
    identity = {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN}
    config = {"publisher": "/publisher", "publisher_sha256": "c" * 64, "policy": {"foreign_fx": "/policy"},
              "policy_sha256": {"foreign_fx": "d" * 64}, "image_id": "sha256:" + "e" * 64,
              "remote_allocation": "/var/lib/market-data/production-runtime/test"}
    paths = module.DOMAIN_CONTRACTS["foreign_fx"]["baseline"]
    raw = b"fresh-formal-snapshot"
    result = {"schema_version": "foreign-fx-baseline-snapshot/1", "domain": "foreign_fx", "producer": identity,
              "policy_sha256": "d" * 64, "image_id": config["image_id"], "allocation_root": config["remote_allocation"],
              "files": {path: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)} for path in paths},
              "contents": {path: base64.b64encode(raw).decode() for path in paths}}
    monkeypatch.setattr(producer.delivery, "_remote_hash", lambda cfg, path: "c" * 64 if path == "/publisher" else "d" * 64)
    calls = []
    def ssh(cfg, command):
        calls.append(command)
        return json.dumps(result).encode()
    monkeypatch.setattr(producer.delivery, "_ssh", ssh)
    work = tmp_path / "first"
    work.mkdir()
    root, files = producer.refresh_baseline(config, work, identity)
    assert (root / module.FX_STABLE).read_bytes() == raw and files == result["files"]
    assert calls[0][-3:] == ["snapshot-baseline", "--policy", "/policy"]
    result["contents"][module.FX_STABLE] = base64.b64encode(b"tampered").decode()
    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(producer.delivery.ProductionDataError, match="bytes differ"):
        producer.refresh_baseline(config, second, identity)


def test_no_change_publication_keeps_quotes_and_evidence_and_receipt_tracks_new_check(monkeypatch, tmp_path):
    import json
    module = host()
    allocation = tmp_path / "allocation"
    contract = module.DOMAIN_CONTRACTS["foreign_fx"]
    formal = allocation / contract["domain_dir"]
    formal.mkdir(parents=True)
    for name in ("daily.json", "source_evidence.json", "status.json"):
        (formal / name).write_bytes(("old-" + name).encode())
    previous = {name: (formal / name).read_bytes() for name in ("daily.json", "source_evidence.json")}
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / module.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    identity = {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN}
    metadata = {"record_count": 8, "latest_dates": {code: "2024-01-10" for code in ("BRL", "CAD", "AUD", "MYR", "IDR", "THB", "INR", "CNY")}, "added": 0, "revised": 0}
    delta = {"schema_version": module.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "fx-no-change",
             "generated_at_utc": "2024-01-11T00:00:00Z", "domain": "foreign_fx", "producer": identity,
             "run": {"run_id": "fx-no-change", "git_head": identity["commit"], "business_status": "no_change", "published": False},
             "baseline": {"files": {path: module._actual_identity(allocation / path) for path in contract["baseline"]}},
             "domain_metadata": metadata, "payloads": {name: module._actual_identity(formal / name) for name in previous}}
    policy = {"policy_id": "fx-policy", "domain": "foreign_fx", "allocation_root": str(allocation), "approved_producer": identity}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "c" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, policy, delta_id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("receipt" + suffix))
    monkeypatch.setattr(module, "read_json", lambda _: delta)
    monkeypatch.setattr(module, "_candidate_files", lambda *_: {})
    monkeypatch.setattr(module, "_load_pass_report", lambda *_: {"candidate_files": {}, "semantic": {"observations": {**metadata, "business_changed": False}}})
    monkeypatch.setattr(module, "_check_baseline", lambda *_: None)
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_git_identity", lambda _: identity)
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    receipt = module.publish("policy", delta["delta_id"], "report", "d" * 64)
    assert receipt["status"] == "NO_CHANGE" and receipt["backup_path"] is None
    assert all((formal / name).read_bytes() == raw for name, raw in previous.items())
    assert json.loads((formal / "status.json").read_bytes())["result"] == "NO_CHANGE"
    assert receipt["formal_files"][module.FX_STATUS] == module._actual_identity(formal / "status.json")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fail_after_exchange", [False, True])
def test_fx_directory_switch_includes_status_and_failed_switch_restores_complete_old_state(monkeypatch, tmp_path, existing, fail_after_exchange):
    import json
    module = host()
    allocation = tmp_path / "allocation"
    contract = module.DOMAIN_CONTRACTS["foreign_fx"]
    formal = allocation / contract["domain_dir"]
    formal.parent.mkdir(parents=True)
    old = {name: ("old-" + name).encode() for name in ("daily.json", "source_evidence.json", "status.json")}
    if existing:
        formal.mkdir()
        for name, raw in old.items():
            (formal / name).write_bytes(raw)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in contract["payloads"]:
        (candidate / name).write_bytes(("new-" + name).encode())
    identity = {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN}
    metadata = {"record_count": 8, "latest_dates": {code: "2024-01-10" for code in ("BRL", "CAD", "AUD", "MYR", "IDR", "THB", "INR", "CNY")}, "added": 8, "revised": 0}
    delta = {"schema_version": module.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "fx-switch",
             "generated_at_utc": "2024-01-11T00:00:00Z", "domain": "foreign_fx", "producer": identity,
             "run": {"run_id": "fx-switch", "git_head": identity["commit"], "business_status": "updated" if existing else "initialized", "published": False},
             "baseline": {"files": {path: module._actual_identity(allocation / path) for path in contract["baseline"]}},
             "domain_metadata": metadata, "payloads": {name: module._actual_identity(candidate / name) for name in contract["payloads"]}}
    (candidate / module.MANIFEST_NAME).write_text(json.dumps(delta), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_bytes(b"{}")
    policy = {"policy_id": "fx-policy", "domain": "foreign_fx", "allocation_root": str(allocation), "approved_producer": identity}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "c" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, parent: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, policy, delta_id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("backup" if root == module.BACKUP_ROOT else "receipt" + suffix))
    monkeypatch.setattr(module, "_candidate_files", lambda *_: {})
    monkeypatch.setattr(module, "_load_pass_report", lambda *_: {"candidate_files": {}, "semantic": {"observations": {**metadata, "business_changed": True}}})
    monkeypatch.setattr(module, "_check_baseline", lambda *_: None)
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    git_checks = []
    def git_identity(_):
        git_checks.append(True)
        return {} if fail_after_exchange and len(git_checks) == 2 else identity
    monkeypatch.setattr(module, "_git_identity", git_identity)
    def exchange(left, right, flag):
        if flag == 1:
            left.rename(right)
        else:
            temporary = left.with_name(left.name + ".swap")
            left.rename(temporary); right.rename(left); temporary.rename(right)
    monkeypatch.setattr(module, "_renameat2", exchange)
    monkeypatch.setattr(module.os, "chown", lambda *_: None, raising=False)
    if fail_after_exchange:
        with pytest.raises(module.DeltaError, match="source clone changed during publication"):
            module.publish("policy", "fx-switch", report, module.sha256_file(report))
        if existing:
            assert all((formal / name).read_bytes() == raw for name, raw in old.items())
        else:
            assert not formal.exists()
        assert not (tmp_path / "receipt.publication.json").exists()
    else:
        receipt = module.publish("policy", "fx-switch", report, module.sha256_file(report))
        assert receipt["status"] == "PUBLISHED"
        assert set(module._file_set(formal)) == set(old)
        assert (formal / "daily.json").read_bytes() == b"new-daily.json"
        assert load_update_status(formal / "daily.json")["result"] == "UPDATED"

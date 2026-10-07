from __future__ import annotations

import base64
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

from jsonschema import Draft202012Validator
import pytest

from agri_research_agent.automation import production_data_delta_canola as producer
from agri_research_agent.pipelines.canada_canola import SOURCE_URLS, import_workbook

ROOT = Path(__file__).resolve().parents[2]


def host():
    spec = importlib.util.spec_from_file_location("canola_publisher_test", ROOT / "09_deploy/production_data_delivery/activate_production_data_delta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(*rows):
    return {"schema_version": "canada-canola/1", "generated_at": "2026-10-07T00:00:00+00:00",
            "records": list(rows), "import_notes": []}


def report_row(module, day="2026-09-28", value=62):
    raw = b"official canola table, provincial harvested percentage"
    return {"province": "SK", "metric": "HARVESTED", "date": day, "value": value,
            "source_url": SOURCE_URLS["SK"], "source_sha256": module.sha256_bytes(raw),
            "source_locator": "Table 1, provincial canola harvested", "date_basis": "report_cutoff",
            "published_at": "2026-10-01", "retrieved_at": "2026-10-07T00:00:00+00:00", "status": "reported"}


def evidence(module, row):
    return {"schema_version": "canada-canola-source-evidence/1", "sources": [{
        "kind": "report", "sha256": row["source_sha256"],
        "bytes_base64": base64.b64encode(b"official canola table, provincial harvested percentage").decode(),
        "province": row["province"], "source_url": row["source_url"], "retrieved_at": row["retrieved_at"]}]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def delta(module):
    contract = module.contract_for_domain("canada_canola")
    identity = {"sha256": "c" * 64, "size_bytes": 12}
    return {"schema_version": module.SCHEMA_VERSION, "status": "CANDIDATE", "delta_id": "canola-delta-001",
            "generated_at_utc": "2026-10-07T00:00:00+00:00", "domain": "canada_canola",
            "producer": {"commit": "a" * 40, "tree": "b" * 40, "origin": producer.delivery.ORIGIN},
            "run": {"run_id": "canola-delta-001", "git_head": "a" * 40, "business_status": "initialized", "published": False},
            "baseline": {"files": dict.fromkeys(contract["baseline"])},
            "domain_metadata": {"record_count": 1, "latest_dates": {"SK/HARVESTED": "2026-09-28"}, "revision_keys": []},
            "payloads": {name: dict(identity) for name in contract["payloads"]}}


def test_canola_contract_and_schema_match_and_reject_incomplete_baseline():
    module = host()
    value = delta(module)
    validator = Draft202012Validator(json.loads((ROOT / "09_deploy/production_data_delivery/delta_contract.schema.json").read_text(encoding="utf-8")))
    assert module.validate_delta_document(value) == value
    validator.validate(value)
    value["baseline"]["files"][module.CANOLA_STABLE] = {"sha256": "d" * 64, "size_bytes": 20}
    with pytest.raises(ValueError, match="together"):
        module.validate_delta_document(value)


@pytest.mark.parametrize("mutation", [None, "extra", "image_tag", "preview_origin", "policy_scope", "relative_candidate"])
def test_manual_canola_configuration_is_closed_and_pins_all_publication_inputs(tmp_path, mutation):
    module = host()
    candidate = write(tmp_path / "candidate.json", {})
    config = {"schema_version": producer.CONFIG_SCHEMA, "approved_commit": "a" * 40,
              "approved_tree": "b" * 40, "origin": producer.delivery.ORIGIN, "python": str(Path(sys.executable).resolve()),
              "runtime_root": str(tmp_path / "runs"), "baseline_root": str(tmp_path / "baseline"),
              "baseline_manifest_sha256": "c" * 64, "candidate_path": str(candidate),
              "candidate_sha256": module.sha256_file(candidate), "source_root": str(tmp_path / "sources"),
              "workbook_path": None, "workbook_sha256": None, "revision_keys": [], "ssh_target": "tencent-market",
              "publisher": "/var/lib/market-data/production-input-producers/approved/09_deploy/production_data_delivery/activate_production_data_delta.py",
              "publisher_sha256": "d" * 64, "image_id": "sha256:" + "e" * 64,
              "remote_allocation": "/var/lib/market-data/production-runtime/test-canola",
              "policy": {"canada_canola": "/etc/market-data/production-data-delivery/canola.json"},
              "policy_sha256": {"canada_canola": "f" * 64}}
    if mutation is None:
        assert producer.validate_config(config) == config
        return
    if mutation == "extra":
        config["bypass"] = True
    elif mutation == "image_tag":
        config["image_id"] = "latest"
    elif mutation == "preview_origin":
        config["origin"] = "file:///preview"
    elif mutation == "policy_scope":
        config["policy"]["soybean_crop_progress"] = "/etc/market-data/production-data-delivery/crop.json"
    else:
        config["candidate_path"] = "candidate.json"
    with pytest.raises(ValueError):
        producer.validate_config(config)


@pytest.mark.parametrize("mutation", ["unknown_payload", "parquet", "duplicate_revision", "wrong_metric", "invalid_date"])
def test_canola_contract_rejects_wrong_file_types_and_revision_scope(mutation):
    module = host()
    value = delta(module)
    if mutation == "unknown_payload":
        value["payloads"]["provider.py"] = value["payloads"]["canola_weekly.json"]
    elif mutation == "parquet":
        value["payloads"]["canola_weekly.json"]["row_count"] = 1
    elif mutation == "duplicate_revision":
        value["domain_metadata"]["revision_keys"] = ["SK/HARVESTED/2026-09-28"] * 2
    elif mutation == "wrong_metric":
        value["domain_metadata"]["latest_dates"] = {"SK/SALES": "2026-09-28"}
    else:
        value["domain_metadata"]["revision_keys"] = ["SK/HARVESTED/2026-02-30"]
    with pytest.raises(ValueError):
        module.validate_delta_document(value)


def test_canola_semantics_append_no_change_and_explicit_revision(tmp_path):
    module = host()
    old = report_row(module, "2026-09-21", 55)
    new = report_row(module)
    baseline = write(tmp_path / "base.json", bundle(old))
    candidate = write(tmp_path / "new.json", bundle(old, new))
    result = module._canola_observations(candidate, baseline, evidence(module, new), [])
    assert (result["added"], result["revised"], result["unchanged"]) == (1, 0, 1)
    empty = {"schema_version": "canada-canola-source-evidence/1", "sources": []}
    assert not module._canola_observations(baseline, baseline, empty, [])["business_changed"]
    corrected = {**old, "value": 56}
    candidate = write(candidate, bundle(corrected))
    with pytest.raises(ValueError, match="exact explicit"):
        module._canola_observations(candidate, baseline, evidence(module, corrected), [])
    result = module._canola_observations(candidate, baseline, evidence(module, corrected), ["SK/HARVESTED/2026-09-21"])
    assert result["revised"] == 1
    with pytest.raises(ValueError, match="exact explicit"):
        module._canola_observations(candidate, baseline, evidence(module, corrected), ["SK/HARVESTED/2026-09-28"])


def test_worker_uses_json_validator_and_checks_actual_metadata_and_image_identity(monkeypatch, tmp_path):
    module = host()
    row = report_row(module)
    candidate = tmp_path / "candidate"
    value = delta(module)
    for name, content in {"canola_weekly.json": bundle(row), "source_evidence.json": evidence(module, row)}.items():
        file = write(candidate / name, content)
        value["payloads"][name] = module._actual_identity(file)
    write(candidate / module.MANIFEST_NAME, value)
    read_json = module.read_json
    monkeypatch.setattr(module, "Path", lambda path: candidate if path == "/candidate" else tmp_path / "allocation" if path == "/allocation" else Path(path))
    monkeypatch.setattr(module, "read_json", lambda path: {"git_commit": "a" * 40, "git_tree": "b" * 40} if str(path) == "/app/RELEASE.json" else read_json(path))
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", "a" * 40)
    monkeypatch.setenv("MARKET_DATA_GIT_TREE", "b" * 40)
    result = module.worker_validate("canada_canola", "a" * 40, "b" * 40)
    assert result["status"] == "PASS" and result["observations"]["added"] == 1
    value["domain_metadata"]["record_count"] = 2
    write(candidate / module.MANIFEST_NAME, value)
    with pytest.raises(ValueError, match="metadata mismatch"):
        module.worker_validate("canada_canola", "a" * 40, "b" * 40)
    with pytest.raises(ValueError, match="RELEASE identity"):
        module.worker_validate("canada_canola", "c" * 40, "b" * 40)


@pytest.mark.parametrize("mutation", ["deleted", "missing_source", "source_bytes", "source_url", "retrieval", "extra_source", "future"])
def test_canola_semantics_fail_closed_on_data_or_evidence_tamper(tmp_path, mutation):
    module = host()
    old = report_row(module, "2026-09-21", 55)
    new = report_row(module)
    sources = evidence(module, new)
    rows = [old, new]
    if mutation == "deleted":
        rows = [new]
    elif mutation == "missing_source":
        sources["sources"] = []
    elif mutation == "source_bytes":
        sources["sources"][0]["bytes_base64"] = base64.b64encode(b"changed").decode()
    elif mutation == "source_url":
        sources["sources"][0]["source_url"] = "https://example.org/table"
    elif mutation == "retrieval":
        sources["sources"][0]["retrieved_at"] = "2026-10-07T01:00:00+00:00"
    elif mutation == "extra_source":
        sources["sources"].append(deepcopy(sources["sources"][0]))
    else:
        rows[1] = {**new, "date": "2099-01-01"}
    baseline = write(tmp_path / "base.json", bundle(old))
    candidate = write(tmp_path / "new.json", bundle(*rows))
    with pytest.raises(ValueError):
        module._canola_observations(candidate, baseline, sources, [])


def tiny_workbook(path):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    names = ["萨省（产量55%）", "阿尔伯塔（产量28%）", "曼省（产量16%）"]
    sheets = [{"C2": "日期", "J2": "Provincial", "CU2": "日期", "CV2": "Provincial", "BR3": "日期", "ID2": "Provincial", "IK2": "收割率", "C3": 46280, "J3": .9},
              {"E1": "日期", "K1": "Alberta", "AB2": "日期", "AH2": "Alberta", "AS2": "日期", "AY2": "Alberta", "E2": 46280, "K2": .8},
              {"C1": "日期", "D1": "曼省", "N2": "日期", "T2": "Provincial", "V1": "日期", "W1": "收割进度", "C2": 46280, "D2": .5}]
    with zipfile.ZipFile(path, "w") as archive:
        xml = f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        for index, (name, cells) in enumerate(zip(names, sheets), 1):
            xml += f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
            content = ''.join(f'<c r="{ref}" t="str"><v>{value}</v></c>' if isinstance(value, str) else f'<c r="{ref}"><v>{value}</v></c>' for ref, value in cells.items())
            archive.writestr(f"xl/worksheets/sheet{index}.xml", f'<worksheet xmlns="{ns}"><sheetData><row>{content}</row></sheetData></worksheet>')
        archive.writestr("xl/workbook.xml", xml + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships>' + ''.join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, 4)) + '</Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"/>')


def test_first_import_recomputes_workbook_and_rejects_omission_or_wrong_value(tmp_path):
    module = host()
    workbook = tmp_path / "source.xlsx"
    tiny_workbook(workbook)
    initial = import_workbook(workbook)
    sources = {"schema_version": "canada-canola-source-evidence/1", "sources": [{
        "kind": "workbook", "sha256": module.sha256_file(workbook), "province": None,
        "source_url": None, "retrieved_at": None, "bytes_base64": base64.b64encode(workbook.read_bytes()).decode()}]}
    candidate = write(tmp_path / "candidate.json", initial)
    assert module._canola_observations(candidate, None, sources, [])["added"] == 3
    omitted = deepcopy(initial)
    omitted["records"].pop()
    write(candidate, omitted)
    with pytest.raises(ValueError, match="omitted"):
        module._canola_observations(candidate, None, sources, [])
    initial["records"][0]["value"] = 1
    write(candidate, initial)
    with pytest.raises(ValueError, match="observation mismatch"):
        module._canola_observations(candidate, None, sources, [])


def test_baseline_requires_pinned_server_snapshot_and_refuses_unexpected_files(tmp_path):
    module = host()
    manifest = {"schema_version": "canada-canola-production-baseline/1", "source_root": "/var/lib/market-data/production-runtime/test-canola", "files": dict.fromkeys(module.DOMAIN_CONTRACTS["canada_canola"]["baseline"])}
    path = write(tmp_path / "baseline_manifest.json", manifest)
    config = {"baseline_root": str(tmp_path), "baseline_manifest_sha256": module.sha256_file(path), "remote_allocation": manifest["source_root"]}
    assert producer.verify_baseline(config) == manifest["files"]
    (tmp_path / "unrelated.json").write_text("{}")
    with pytest.raises(ValueError, match="unexpected"):
        producer.verify_baseline(config)


def test_canola_publish_preserves_unrelated_files_and_restores_on_status_failure(monkeypatch, tmp_path):
    module = host()
    allocation = tmp_path / "allocation"
    formal = allocation / module.DOMAIN_CONTRACTS["canada_canola"]["domain_dir"]
    formal.mkdir(parents=True)
    (formal / "notes.txt").write_bytes(b"retain")
    status = write(allocation / module.CANOLA_STATUS, {"old": True})
    old_status = status.read_bytes()
    candidate = tmp_path / "package"
    value = delta(module)
    for name in value["payloads"]:
        file = write(candidate / name, {"payload": name})
        value["payloads"][name] = module._actual_identity(file)
    write(candidate / module.MANIFEST_NAME, value)
    report_path = write(tmp_path / "report.json", {})
    policy = {"policy_id": "canola-policy", "domain": "canada_canola", "allocation_root": str(allocation), "approved_producer": value["producer"]}
    monkeypatch.setattr(module, "_load_policy", lambda _: (policy, "f" * 64))
    monkeypatch.setattr(module, "_protected", lambda path, **_: Path(path))
    monkeypatch.setattr(module, "_protected_tree", lambda path: Path(path))
    monkeypatch.setattr(module, "_under", lambda path, _root, **_: Path(path))
    monkeypatch.setattr(module, "_derived", lambda root, _policy, _id, suffix="": candidate if root == module.CANDIDATE_ROOT else tmp_path / ("backup" if root == module.BACKUP_ROOT else "evidence" + suffix))
    monkeypatch.setattr(module, "_load_pass_report", lambda *_: {"candidate_files": module._candidate_files(candidate, value), "semantic": {"observations": {"business_changed": True, "record_count": 1, "latest_dates": value["domain_metadata"]["latest_dates"], "added": 1, "revised": 0}}})
    monkeypatch.setattr(module, "_check_baseline", lambda *_: None)
    monkeypatch.setattr(module, "_policy_unchanged", lambda *_: None)
    monkeypatch.setattr(module, "_git_identity", lambda _: value["producer"])
    monkeypatch.setattr(module, "_lock", lambda _: nullcontext())
    monkeypatch.setattr(module.os, "chown", lambda *_: None, raising=False)
    def exchange(left, right, _flag):
        temporary = left.with_name(left.name + ".swap")
        left.rename(temporary); right.rename(left); temporary.rename(right)
    monkeypatch.setattr(module, "_renameat2", exchange)
    monkeypatch.setattr(module, "_atomic_json", lambda *_: (_ for _ in ()).throw(OSError("status write")))
    with pytest.raises(OSError, match="status write"):
        module.publish(tmp_path / "policy", value["delta_id"], report_path, module.sha256_file(report_path))
    assert (formal / "notes.txt").read_bytes() == b"retain"
    assert not (formal / "canola_weekly.json").exists()
    assert status.read_bytes() == old_status
    assert (tmp_path / "backup/domain/notes.txt").read_bytes() == b"retain"

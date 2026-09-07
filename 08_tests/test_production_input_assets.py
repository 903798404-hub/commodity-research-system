from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from jsonschema import Draft202012Validator

from agri_research_agent.import_profit.runtime_store import (
    file_sha256,
    load_runtime_release_dataset,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    bootstrap_import_profit_runtime,
)
from test_import_profit_runtime_store import CONFIG, CONFIG_PATH, make_historical_candidate


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "09_deploy" / "production_inputs" / "asset_tool.py"
SPEC = importlib.util.spec_from_file_location("production_input_asset_tool", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
asset_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(asset_tool)

NOW = datetime(2026, 9, 7, 1, 2, 3, tzinfo=timezone.utc)


def _stage_a_source(tmp_path: Path) -> tuple[Path, Path, dict]:
    source_candidate = make_historical_candidate(tmp_path / "stage")
    runtime = tmp_path / "stage-a-runtime"
    bootstrap_import_profit_runtime(
        source_candidate, runtime, config=CONFIG, config_path=CONFIG_PATH,
        release_id="stage-a-001", generated_at=NOW,
    )
    resolved = resolve_current_runtime_release(runtime)
    source = {
        "runtime_root": str(runtime),
        "index_sha256": file_sha256(runtime / "release_index.json").lower(),
        "release_manifest_sha256": file_sha256(resolved.manifest_path).lower(),
        "source_manifest_path": str(source_candidate / "manifest.json"),
        "source_manifest_sha256": file_sha256(source_candidate / "manifest.json").lower(),
        "payload_sha256": {
            name: file_sha256(resolved.release_dir / name).lower()
            for name in asset_tool.PAYLOAD_NAMES
        },
    }
    return runtime, source_candidate, source


def _historical_scope(tmp_path: Path, source: dict) -> dict:
    return {
        "kind": "historical", "runtime_id": "formal-runtime-001",
        "release_id": "formal-release-001",
        "candidate_path": str(tmp_path / "formal-candidate"),
        "formal_path": str(tmp_path / "formal-published"),
        "source": source,
    }


def _approved_scope() -> dict:
    commit = "a" * 40
    return {
        "kind": "historical", "runtime_id": "formal-runtime-001",
        "release_id": "formal-release-001",
        "candidate_path": "/var/lib/market-data/production-input-candidates/formal-001",
        "formal_path": "/var/lib/market-data/production-input-assets/formal-001",
        "producer": {"commit": commit, "tree": "b" * 40},
        "helper_image": {"image_id": "sha256:" + "c" * 64, "commit": commit, "tree": "b" * 40},
        "source": {
            "runtime_root": "/srv/stage-a/runtime", "source_manifest_path": "/srv/stage-a/manifest.json",
            "source_manifest_sha256": "d" * 64, "release_manifest_sha256": "e" * 64,
            "index_sha256": "f" * 64, "payload_sha256": {name: "1" * 64 for name in asset_tool.PAYLOAD_NAMES},
        },
    }


def _initial_approval(scope=None) -> dict:
    return {
        "schema_version": "production-input-approval/1", "approval_id": "approval-formal-001",
        "state": "historical_initialization_approved",
        "approved_at": "2026-09-07T01:00:00+00:00", "approved_by": "production-operator",
        "task_evidence_sha256": "9" * 64,
        "scope": scope or _approved_scope(), "asset_manifest_sha256": None,
        "prior_approval_sha256": None,
    }


def test_reference_json_schemas_accept_the_valid_approval_contract():
    for filename in asset_tool.SCHEMAS.values():
        Draft202012Validator.check_schema(
            json.loads((TOOL_PATH.parent / filename).read_text(encoding="utf-8"))
        )
    schema = json.loads((TOOL_PATH.parent / asset_tool.SCHEMAS["approval"]).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_initial_approval())


@pytest.mark.parametrize("change", ["preview", "same_destination", "initial_asset_sha", "state_kind", "unknown_field"])
def test_approval_document_rejects_preview_destinations_and_invalid_evidence_states(change):
    approval = _initial_approval()
    if change == "preview":
        approval["scope"]["candidate_path"] = "/var/lib/market-data/production-input-candidates/preview-001"
    elif change == "same_destination":
        approval["scope"]["formal_path"] = approval["scope"]["candidate_path"]
    elif change == "initial_asset_sha":
        approval["asset_manifest_sha256"] = "0" * 64
    elif change == "state_kind":
        approval["state"] = "cnf_extraction_approved"
    else:
        approval["unapproved"] = True

    with pytest.raises(asset_tool.AssetError):
        asset_tool.approval_document(approval)


def test_asset_approval_requires_two_lowercase_hashes_after_validation():
    approval = _initial_approval()
    approval.update(state="asset_approved", asset_manifest_sha256="a" * 64,
                    prior_approval_sha256="b" * 64)

    assert asset_tool.approval_document(approval) is approval
    approval["prior_approval_sha256"] = "A" * 64
    with pytest.raises(asset_tool.AssetError, match="prior_approval_sha256: invalid pattern"):
        asset_tool.approval_document(approval)


def test_validate_asset_files_rejects_schema_unknown_field_before_any_publication(tmp_path):
    scope = _approved_scope()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    asset = {
        "schema_version": "production-historical-runtime/1", "kind": "historical",
        "runtime_id": scope["runtime_id"], "release_id": scope["release_id"],
        "generated_at": "2026-09-07T01:00:00+00:00", "producer": scope["producer"],
        "helper_image": scope["helper_image"], "initial_approval_sha256": "a" * 64,
        "candidate_path": scope["candidate_path"], "intended_formal_path": scope["formal_path"],
        "readonly_consumption": True, "source": scope["source"],
        "observations": {"manifest_sha256": "b" * 64, "index_sha256": "c" * 64,
                         "source_bytes_mutated": False, "historical_values_recalculated": False,
                         "record_count": 0, "date_range": ["2026-01-01", "2026-01-01"]},
        "payloads": {}, "preview_producer": True,
    }
    (candidate / asset_tool.ASSET_MANIFEST).write_text(json.dumps(asset), encoding="utf-8")
    initial = _initial_approval(scope)

    with pytest.raises(asset_tool.AssetError, match="unknown field"):
        asset_tool.validate_asset_files(candidate, initial, "a" * 64)


def test_historical_initialization_copies_exact_payload_bytes_and_real_consumer_loads(tmp_path):
    _, _, source = _stage_a_source(tmp_path)
    scope = _historical_scope(tmp_path, source)

    result = asset_tool.initialize_history_files(
        scope, "2026-09-07T01:02:03+00:00"
    )

    candidate = Path(scope["candidate_path"])
    formal_release = candidate / "releases" / scope["release_id"]
    for name, expected in source["payload_sha256"].items():
        assert file_sha256(formal_release / name).lower() == expected
    assert result["source_bytes_mutated"] is False
    assert result["historical_values_recalculated"] is False
    assert (formal_release / "manual_cnf_quotes.parquet").exists() is False
    loaded = load_runtime_release_dataset(candidate)
    assert loaded.resolved.release_id == scope["release_id"]
    assert loaded.dataset.business_key_count == result["record_count"]


def test_historical_initialization_rejects_source_hash_drift_without_creating_candidate(tmp_path):
    runtime, _, source = _stage_a_source(tmp_path)
    scope = _historical_scope(tmp_path, source)
    (runtime / "release_index.json").write_text("{}", encoding="utf-8")

    with pytest.raises(asset_tool.AssetError, match="Source index SHA mismatch"):
        asset_tool.initialize_history_files(scope, "2026-09-07T01:02:03+00:00")

    assert not Path(scope["candidate_path"]).exists()


def test_cnf_frame_preserves_decimal_null_zero_and_database_updated_at():
    timestamp = datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)
    rows = [
        {"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf", "month": 10, "cnf": None, "updated_at": timestamp},
        {"trade_date": date(2026, 9, 1), "region_hex": "e7be8ee6b9be", "month": 10, "cnf": Decimal("0.00"), "updated_at": timestamp},
        {"trade_date": date(2026, 9, 2), "region_hex": "e7be8ee8a5bf", "month": 11, "cnf": Decimal("123.4500"), "updated_at": timestamp},
    ]
    frame = asset_tool.cnf_frame(rows)

    assert frame.loc[0, "cnf"] is None
    assert frame.loc[1, "cnf"] == Decimal("0.00")
    assert type(frame.loc[2, "cnf"]) is Decimal
    assert list(frame["updated_at"]) == [timestamp, timestamp, timestamp]
    assert list(frame["origin"]) == ["brazil", "us_gulf", "us_pnw"]


@pytest.mark.parametrize("bad_rows", [
    [{"trade_date": date(2026, 9, 1), "region_hex": "unknown", "month": 10, "cnf": None, "updated_at": None}],
    [
        {"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf", "month": 10, "cnf": None, "updated_at": None},
        {"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf", "month": 10, "cnf": Decimal("0"), "updated_at": None},
    ],
])
def test_cnf_frame_rejects_unknown_or_duplicate_production_natural_keys(bad_rows):
    with pytest.raises(asset_tool.AssetError):
        asset_tool.cnf_frame(bad_rows)


def test_inspect_cnf_rejects_preview_identity_and_keeps_nullable_decimal_contract(tmp_path):
    cache = tmp_path / asset_tool.CACHE
    table = pa.Table.from_pylist([
        {"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf", "month": 10,
         "cnf": Decimal("12.3400"), "updated_at": None, "region": "巴西", "origin": "brazil",
         "source_identity": asset_tool.SOURCE_IDENTITY},
        {"trade_date": date(2026, 9, 2), "region_hex": "e7be8ee6b9be", "month": 10,
         "cnf": None, "updated_at": None, "region": "美湾", "origin": "us_gulf",
         "source_identity": asset_tool.SOURCE_IDENTITY},
    ])
    pq.write_table(table, cache)
    metrics = asset_tool.inspect_cnf(cache)
    assert metrics["null_cnf_count"] == 1 and metrics["zero_cnf_count"] == 0
    assert metrics["null_updated_at_count"] == 2

    rows = table.to_pylist()
    rows[0]["source_identity"] = "final-ui-preview-cnf-cache-v1"
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), cache)
    with pytest.raises(asset_tool.AssetError, match="Preview/foreign"):
        asset_tool.inspect_cnf(cache)


@pytest.mark.parametrize("actual_database", ["quanyong", "wrong_database"])
def test_extract_cnf_requires_readonly_proof_before_query_and_actual_database_identity(
    tmp_path, monkeypatch, actual_database
):
    import psycopg
    from agri_research_agent.data_sources.tankan.client import TankanConnectionSettings

    events = []
    settings = SimpleNamespace(
        database="quanyong", host="host", port=5432, user="user", password="secret",
        clear_password=lambda: events.append("password_cleared"),
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            events.append(statement)

        def fetchone(self):
            statement = events[-1]
            if statement == "SHOW transaction_read_only":
                return {"transaction_read_only": "on"}
            if statement == "SHOW default_transaction_read_only":
                return {"default_transaction_read_only": "on"}
            return {"database": actual_database, "relation_oid": 42, "server_version": "16.0"}

        def fetchall(self):
            return [{"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf",
                     "month": 10, "cnf": Decimal("12.3400"), "updated_at": None}]

    class Connection:
        read_only = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

        def rollback(self):
            events.append("rollback")

    monkeypatch.setattr(TankanConnectionSettings, "from_secret_file", lambda _: settings)
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: Connection())
    output = tmp_path / asset_tool.CACHE
    if actual_database == "quanyong":
        result = asset_tool.extract_cnf(tmp_path / "secret.env", output)
        assert result["readonly_transaction"]["rolled_back"] is True
        assert output.is_file()
        assert events.index(asset_tool.QUERY) > events.index("SHOW default_transaction_read_only")
    else:
        with pytest.raises(asset_tool.AssetError, match="database identity"):
            asset_tool.extract_cnf(tmp_path / "secret.env", output)
        assert asset_tool.QUERY not in events
    assert events[-1] == "password_cleared"


def _publication_fixture(tmp_path, monkeypatch):
    """Exercise publication sequencing without a host-root or Docker fixture."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / asset_tool.ASSET_MANIFEST).write_text("{}", encoding="utf-8")
    (candidate / "payload.bin").write_bytes(b"immutable-payload")
    formal = tmp_path / "formal"
    report_path = tmp_path / "validation.json"
    receipt_path = tmp_path / "receipt.json"
    scope = {
        "kind": "historical", "runtime_id": "formal-runtime-001",
        "release_id": "formal-release-001", "candidate_path": str(candidate),
        "formal_path": str(formal), "helper_image": {"image_id": "sha256:" + "a" * 64,
        "commit": "a" * 40, "tree": "b" * 40}, "producer": {"commit": "a" * 40, "tree": "b" * 40},
        "source": {},
    }
    initial = {"state": "historical_initialization_approved", "scope": scope}
    asset_approval = {"state": "asset_approved", "scope": scope,
                      "prior_approval_sha256": "", "asset_manifest_sha256": "",
                      "approved_at": "2026-09-07T02:00:00+00:00"}
    initial_path = tmp_path / "initial.json"
    approval_path = tmp_path / "asset-approval.json"
    initial_path.write_text("initial", encoding="utf-8")
    approval_path.write_text("asset", encoding="utf-8")
    asset_approval["prior_approval_sha256"] = asset_tool.sha_file(initial_path)
    asset_approval["asset_manifest_sha256"] = asset_tool.sha_file(candidate / asset_tool.ASSET_MANIFEST)
    report = {"schema_version": "production-input-validation/1", "status": "PASS",
              "validated_at": "2026-09-07T01:30:00+00:00", "producer": scope["producer"],
              "helper_image": scope["helper_image"], "candidate_path": str(candidate),
              "initial_approval_sha256": asset_tool.sha_file(initial_path), "files": asset_tool.file_set(candidate)}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(asset_tool, "load_approval", lambda path: asset_approval if Path(path) == approval_path else initial)
    monkeypatch.setattr(asset_tool, "protected", lambda path, **kwargs: Path(path))
    monkeypatch.setattr(asset_tool, "protected_tree", lambda path: None)
    monkeypatch.setattr(asset_tool, "under", lambda path, parent: Path(path))
    monkeypatch.setattr(asset_tool, "git_identity", lambda: scope["producer"])
    monkeypatch.setattr(asset_tool, "validate_asset_files", lambda *args: {})
    monkeypatch.setattr(
        asset_tool, "atomic_directory_publish",
        lambda staging, destination: Path(staging).rename(destination),
    )
    return approval_path, initial_path, report_path, receipt_path, candidate, formal, report, asset_approval


@pytest.mark.parametrize("failure", ["extraction", "approval_sha", "report_drift", "formal_exists"])
def test_publish_rejects_incomplete_or_changed_authority_chain(tmp_path, monkeypatch, failure):
    approval, initial, report_path, receipt, candidate, formal, report, asset = _publication_fixture(tmp_path, monkeypatch)
    if failure == "extraction":
        asset["state"] = "historical_initialization_approved"
    elif failure == "approval_sha":
        asset["prior_approval_sha256"] = "0" * 64
    elif failure == "report_drift":
        report["files"] = {}
        report_path.write_text(json.dumps(report), encoding="utf-8")
    else:
        formal.mkdir()

    reason = {"extraction": "Separate exact asset approval", "approval_sha": "Initial approval chain mismatch",
              "report_drift": "Candidate changed since validation", "formal_exists": "Formal destination already exists"}[failure]
    with pytest.raises(asset_tool.AssetError, match=reason):
        asset_tool.publish(approval, initial, report_path, receipt)

    assert not receipt.exists()


def test_publish_copies_exact_bytes_and_refuses_later_overwrite(tmp_path, monkeypatch):
    approval, initial, report_path, receipt, candidate, formal, _, _ = _publication_fixture(tmp_path, monkeypatch)

    published = asset_tool.publish(approval, initial, report_path, receipt)

    assert published["status"] == "PUBLISHED"
    assert asset_tool.file_set(formal) == asset_tool.file_set(candidate)
    assert (formal / "payload.bin").read_bytes() == b"immutable-payload"
    with pytest.raises(asset_tool.AssetError, match="already exists"):
        asset_tool.publish(approval, initial, report_path, tmp_path / "second-receipt.json")


def test_publish_copy_failure_keeps_formal_absent_and_retains_staging(tmp_path, monkeypatch):
    approval, initial, report_path, receipt, _, formal, _, _ = _publication_fixture(tmp_path, monkeypatch)

    def fail_copy(path, raw):
        if ".publishing-" in str(path):
            raise OSError("injected staged-copy failure")
        raise AssertionError("unexpected write target")

    monkeypatch.setattr(asset_tool, "write_exclusive", fail_copy)
    with pytest.raises(OSError, match="staged-copy"):
        asset_tool.publish(approval, initial, report_path, receipt)

    assert not formal.exists()
    assert list(formal.parent.glob(".publishing-*"))
    assert not receipt.exists()


def test_decimal_numeric_equivalence_is_checked_without_float_conversion():
    common = {"trade_date": date(2026, 9, 1), "region_hex": "e5b7b4e8a5bf",
              "month": 10, "updated_at": None}
    one = [{**common, "cnf": Decimal("123.4500")}]
    padded = [{**common, "cnf": Decimal("123.450000")}]
    changed = [{**common, "cnf": Decimal("123.4501")}]

    assert asset_tool.canonical_rows(one) == asset_tool.canonical_rows(padded)
    assert asset_tool.canonical_rows(one) != asset_tool.canonical_rows(changed)

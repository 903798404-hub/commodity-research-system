from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "09_deploy" / "usda_release" / "usda_runtime_data.py"
SPEC = importlib.util.spec_from_file_location("usda_runtime_data", SCRIPT)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def source_data(root: Path, month: str = "2026-08", previous: str = "2026-07") -> Path:
    source = root / "source"
    matrices = [
        {"category": "Oilseeds", "commodity": "Oilseed, Soybean", "commodityCode": "2222000", "country": "United States", "countryCode": "US", "file": "matrix/2222000_US.json"},
        {"category": "Oils", "commodity": "Oil, Palm", "commodityCode": "4243000", "country": "World", "countryCode": "GL", "file": "matrix/4243000_GL.json"},
    ]
    write_json(source / "index.json", {"matrices": matrices})
    write_json(source / "report_version.json", {"currentReportMonth": month, "previousReportMonth": previous})
    write_json(source / "presentation_changes.json", {"snapshotCurrent": month, "snapshotPrevious": previous, "matrices": []})
    for item in matrices:
        write_json(source / item["file"], {"commodity": item["commodity"], "country": item["country"], "years": [2026], "rows": []})
    write_json(source / "snapshots" / "usda_psd" / previous / "index.json", {"matrices": matrices})
    for item in matrices:
        write_json(source / "snapshots" / "usda_psd" / previous / item["file"], {"commodity": item["commodity"], "country": item["country"], "years": [2026], "rows": []})
    write_json(source / "soybean_oil_US.json", {"legacy": True})
    return source


def package(root: Path, source: Path | None = None) -> Path:
    output = root / "packages"
    output.mkdir(parents=True)
    return runtime.build_runtime_release(
        source_data=source or source_data(root),
        output_parent=output,
        builder_git_sha="a" * 40,
        builder_tree_sha="b" * 40,
        fetch_run_id="20260813T020147607Z",
        source_manifest_sha256="c" * 64,
        created_at="2026-08-13T00:00:00Z",
    )


def runtime_root(root: Path) -> Path:
    result = root / "runtime"
    for name in ("incoming", "releases", "failed", "evidence"):
        (result / name).mkdir(parents=True)
    return result


def stage(release: Path, root: Path) -> Path:
    target = root / "incoming" / release.name
    shutil.copytree(release, target)
    return target


def test_manifest_identity_is_stable_minimal_and_seed_uses_exact_input_bytes(tmp_path: Path) -> None:
    first = package(tmp_path / "a")
    second_root = tmp_path / "b"
    second = package(second_root)
    one = runtime.validate_runtime_release(first, app_contract_version=1, supported_data_schema_version=1)
    two = runtime.validate_runtime_release(second, app_contract_version=1, supported_data_schema_version=1)
    assert one["bundle_sha256"] == two["bundle_sha256"]
    assert first.name == second.name
    assert one["matrix_count"] == 2
    paths = {item["path"] for item in one["files"]}
    assert "soybean_oil_US.json" not in paths
    assert paths == {
        "index.json", "report_version.json", "presentation_changes.json",
        "matrix/2222000_US.json", "matrix/4243000_GL.json",
        "snapshots/usda_psd/2026-07/index.json",
        "snapshots/usda_psd/2026-07/matrix/2222000_US.json",
        "snapshots/usda_psd/2026-07/matrix/4243000_GL.json",
    }
    semantic_copy = tmp_path / "semantic-copy"
    shutil.copytree(first / "data", semantic_copy)
    assert runtime.compare_json_semantics(first / "data", semantic_copy)["status"] == "equivalent"
    write_json(semantic_copy / "matrix" / "2222000_US.json", {"changed": True})
    assert runtime.compare_json_semantics(first / "data", semantic_copy)["changed"] == ["matrix/2222000_US.json"]


def test_stage_copies_validated_bytes_and_run_directories_remain_isolated(tmp_path: Path) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    staged = runtime.stage_runtime_release(source_release=release, runtime_root=root, app_contract_version=1, supported_data_schema_version=1)
    assert staged.parent == root / "incoming"
    assert runtime.sha256_file(staged / "release_manifest.json") == runtime.sha256_file(release / "release_manifest.json")
    with pytest.raises(runtime.RuntimeDataError, match="already exists"):
        runtime.stage_runtime_release(source_release=release, runtime_root=root, app_contract_version=1, supported_data_schema_version=1)


@pytest.mark.parametrize("mutation", ["missing", "extra", "invalid_json", "nan"])
def test_validation_rejects_incomplete_or_invalid_bundle(tmp_path: Path, mutation: str) -> None:
    release = package(tmp_path)
    if mutation == "missing":
        (release / "data" / "matrix" / "2222000_US.json").unlink()
    elif mutation == "extra":
        write_json(release / "data" / "extra.json", {})
    elif mutation == "invalid_json":
        (release / "data" / "index.json").write_text("{", encoding="utf-8")
    else:
        (release / "data" / "index.json").write_text('{"value":NaN}\n', encoding="utf-8")
    with pytest.raises((runtime.RuntimeDataError, json.JSONDecodeError, ValueError)):
        runtime.validate_runtime_release(release, app_contract_version=1, supported_data_schema_version=1)


def test_validation_rejects_symlink_hardlink_and_unsafe_manifest_path(tmp_path: Path) -> None:
    hardlink = package(tmp_path / "hardlink")
    try:
        (hardlink / "data" / "hard.json").hardlink_to(hardlink / "data" / "index.json")
    except OSError:
        pytest.skip("this filesystem cannot create hardlinks")
    with pytest.raises(runtime.RuntimeDataError, match="hardlink"):
        runtime.validate_runtime_release(hardlink, app_contract_version=1, supported_data_schema_version=1)

    unsafe = package(tmp_path / "unsafe")
    manifest_path = unsafe / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.json"
    write_json(manifest_path, manifest)
    with pytest.raises(runtime.RuntimeDataError, match="unsafe manifest path"):
        runtime.validate_runtime_release(unsafe, app_contract_version=1, supported_data_schema_version=1)

    symlink = package(tmp_path / "symlink")
    try:
        (symlink / "data" / "link.json").symlink_to(symlink / "data" / "index.json")
    except OSError:
        pytest.skip("this Windows account cannot create symlinks")
    with pytest.raises(runtime.RuntimeDataError, match="symlink"):
        runtime.validate_runtime_release(symlink, app_contract_version=1, supported_data_schema_version=1)


def test_compatibility_gate_rejects_schema_and_app_contract(tmp_path: Path) -> None:
    release = package(tmp_path)
    with pytest.raises(runtime.RuntimeDataError, match="schema"):
        runtime.validate_runtime_release(release, app_contract_version=1, supported_data_schema_version=2)
    with pytest.raises(runtime.RuntimeDataError, match="app contract"):
        runtime.validate_runtime_release(release, app_contract_version=0, supported_data_schema_version=1)


def test_candidate_failure_cannot_promote_and_existing_current_is_unchanged(tmp_path: Path) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    candidate = stage(release, root)
    before = {"release_id": "usda-2026-07-1111111111111111", "report_month": "2026-07", "previous_report_month": "2026-06", "data_schema_version": 1, "minimum_app_contract_version": 1, "bundle_sha256": "d" * 64}
    write_json(root / "current.json", before)
    with pytest.raises(runtime.RuntimeDataError, match="candidate validation"):
        runtime.promote_runtime_release(runtime_root=root, candidate=candidate, app_contract_version=1, supported_data_schema_version=1, http_check=lambda _release: False, evidence_path=root / "evidence" / "failed.json")
    assert json.loads((root / "current.json").read_text(encoding="utf-8")) == before
    assert not (root / "current").exists()
    assert (root / "failed" / f"{release.name}__http-candidate-failed").is_dir()


def test_post_promote_failure_restores_pointer_and_quarantines_failed_release(tmp_path: Path) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    previous = {"release_id": "usda-2026-07-1111111111111111", "report_month": "2026-07", "previous_report_month": "2026-06", "data_schema_version": 1, "minimum_app_contract_version": 1, "bundle_sha256": "d" * 64}
    write_json(root / "current.json", previous)
    (root / "current").write_text(previous["release_id"] + "\n", encoding="utf-8")
    calls = 0

    def first_only(_release: str) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    with pytest.raises(runtime.RuntimeDataError, match="post-promote"):
        runtime.promote_runtime_release(runtime_root=root, candidate=stage(release, root), app_contract_version=1, supported_data_schema_version=1, http_check=first_only, evidence_path=root / "evidence" / "failed.json")
    assert json.loads((root / "current.json").read_text(encoding="utf-8")) == previous
    assert (root / "current").read_text(encoding="utf-8").strip() == previous["release_id"]
    assert (root / "failed" / f"{release.name}__post-promote-failed").is_dir()


def test_promote_is_immutable_preserves_previous_and_duplicate_release_fails(tmp_path: Path) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    previous = {"release_id": "usda-2026-07-1111111111111111", "report_month": "2026-07", "previous_report_month": "2026-06", "data_schema_version": 1, "minimum_app_contract_version": 1, "bundle_sha256": "d" * 64}
    write_json(root / "current.json", previous)
    (root / "current").write_text(previous["release_id"] + "\n", encoding="utf-8")
    result = runtime.promote_runtime_release(runtime_root=root, candidate=stage(release, root), app_contract_version=1, supported_data_schema_version=1, http_check=lambda _release: True, evidence_path=root / "evidence" / "promote.json")
    assert result["status"] == "promoted"
    assert (root / "previous").read_text(encoding="utf-8").strip() == previous["release_id"]
    assert (root / "current").read_text(encoding="utf-8").strip() == release.name
    assert (root / "releases" / release.name).is_dir()
    duplicate = root / "incoming" / release.name
    shutil.copytree(root / "releases" / release.name, duplicate)
    with pytest.raises(runtime.RuntimeDataError, match="already exists"):
        runtime.promote_runtime_release(runtime_root=root, candidate=duplicate, app_contract_version=1, supported_data_schema_version=1, http_check=lambda _release: True, evidence_path=root / "evidence" / "duplicate.json")


def test_formal_release_permissions_are_read_only_and_content_stable(tmp_path: Path) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    candidate = stage(release, root)
    candidate.chmod(0o700)
    for path in candidate.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    before_hashes = {relative: runtime.sha256_file(path) for relative, path in runtime._iter_regular_files(candidate)}
    before_bundle = runtime.bundle_sha256(runtime.file_records(candidate / "data"))
    observed: dict[str, object] = {}

    def permissions_ready(release_id: str) -> bool:
        formal = root / "releases" / release_id
        runtime.validate_release_permissions(formal)
        observed["checked"] = True
        return True

    runtime.promote_runtime_release(runtime_root=root, candidate=candidate, app_contract_version=1, supported_data_schema_version=1, http_check=permissions_ready, evidence_path=root / "evidence" / "permissions.json")
    formal = root / "releases" / release.name
    assert observed == {"checked": True}
    if os.name != "nt":
        assert stat.S_IMODE(formal.stat().st_mode) == 0o755
        for path in formal.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == (0o755 if path.is_dir() else 0o644)
            assert mode & 0o022 == 0
    after_hashes = {relative: runtime.sha256_file(path) for relative, path in runtime._iter_regular_files(formal)}
    assert after_hashes == before_hashes
    assert runtime.bundle_sha256(runtime.file_records(formal / "data")) == before_bundle


def test_permission_preparation_failure_does_not_promote_or_change_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = package(tmp_path / "bundle")
    root = runtime_root(tmp_path)
    before = {"release_id": "usda-2026-07-1111111111111111", "report_month": "2026-07", "previous_report_month": "2026-06", "data_schema_version": 1, "minimum_app_contract_version": 1, "bundle_sha256": "d" * 64}
    write_json(root / "current.json", before)
    candidate = stage(release, root)
    original_chmod = Path.chmod

    def fail_chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == candidate:
            raise PermissionError("simulated chmod failure")
        original_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(runtime.RuntimeDataError, match="normalize release permissions"):
        runtime.promote_runtime_release(runtime_root=root, candidate=candidate, app_contract_version=1, supported_data_schema_version=1, http_check=lambda _release: True, evidence_path=root / "evidence" / "permissions-failed.json")
    assert json.loads((root / "current.json").read_text(encoding="utf-8")) == before
    assert candidate.is_dir()
    assert not (root / "releases" / release.name).exists()


def test_rollback_switches_data_only_and_post_check_failure_restores_current(tmp_path: Path) -> None:
    root = runtime_root(tmp_path)
    release_a = package(tmp_path / "a", source_data(tmp_path / "a", "2026-07", "2026-06"))
    release_b = package(tmp_path / "b", source_data(tmp_path / "b", "2026-08", "2026-07"))
    shutil.copytree(release_a, root / "releases" / release_a.name)
    shutil.copytree(release_b, root / "releases" / release_b.name)
    manifest_b = runtime.validate_runtime_release(release_b, app_contract_version=1, supported_data_schema_version=1)
    write_json(root / "current.json", runtime.current_pointer(manifest_b, updated_at="2026-08-13T00:00:00Z"))
    (root / "current").write_text(release_b.name + "\n", encoding="utf-8")
    (root / "previous").write_text(release_a.name + "\n", encoding="utf-8")
    result = runtime.rollback_runtime_release(runtime_root=root, app_contract_version=1, supported_data_schema_version=1, http_check=lambda release_id: release_id == release_a.name, evidence_path=root / "evidence" / "rollback.json")
    assert result["status"] == "rolled_back"
    assert (root / "current").read_text(encoding="utf-8").strip() == release_a.name
    with pytest.raises(runtime.RuntimeDataError, match="post-check"):
        runtime.rollback_runtime_release(runtime_root=root, app_contract_version=1, supported_data_schema_version=1, http_check=lambda _release: False, evidence_path=root / "evidence" / "rollback-failed.json")
    assert (root / "current").read_text(encoding="utf-8").strip() == release_a.name


def test_ab_release_switch_keeps_old_pinned_url_readable_without_server_restart(tmp_path: Path) -> None:
    root = runtime_root(tmp_path)
    release_a = package(tmp_path / "a", source_data(tmp_path / "a", "2026-07", "2026-06"))
    release_b = package(tmp_path / "b", source_data(tmp_path / "b", "2026-08", "2026-07"))
    shutil.copytree(release_a, root / "releases" / release_a.name)
    manifest_a = runtime.validate_runtime_release(release_a, app_contract_version=1, supported_data_schema_version=1)
    write_json(root / "current.json", runtime.current_pointer(manifest_a, updated_at="2026-08-13T00:00:00Z"))
    (root / "current").write_text(release_a.name + "\n", encoding="utf-8")

    requests: list[str] = []

    def stable_parent_http_check(release_id: str) -> bool:
        requests.append(release_id)
        report = root / "releases" / release_id / "data" / "report_version.json"
        return report.is_file() and json.loads(report.read_text(encoding="utf-8"))["currentReportMonth"] in {"2026-07", "2026-08"}

    pinned_a = root / "releases" / release_a.name / "data" / "report_version.json"
    assert json.loads(pinned_a.read_text(encoding="utf-8"))["currentReportMonth"] == "2026-07"
    runtime.promote_runtime_release(runtime_root=root, candidate=stage(release_b, root), app_contract_version=1, supported_data_schema_version=1, http_check=stable_parent_http_check, evidence_path=root / "evidence" / "ab-promote.json")
    assert json.loads((root / "current.json").read_text(encoding="utf-8"))["release_id"] == release_b.name
    assert json.loads(pinned_a.read_text(encoding="utf-8"))["currentReportMonth"] == "2026-07"
    assert requests == [release_b.name, release_b.name]
    runtime.rollback_runtime_release(runtime_root=root, app_contract_version=1, supported_data_schema_version=1, http_check=stable_parent_http_check, evidence_path=root / "evidence" / "ab-rollback.json")
    assert json.loads((root / "current.json").read_text(encoding="utf-8"))["release_id"] == release_a.name

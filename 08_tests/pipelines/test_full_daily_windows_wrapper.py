from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from filelock import FileLock

from agri_research_agent.automation import full_daily_windows as wrapper


def _manifest(run_id: str, *, business: str = "UPDATED", succeeded: bool = True, prewarm: str = "PASS") -> dict[str, object]:
    return {
        "schema_version": wrapper.DAILY_SCHEMA,
        "run_id": run_id,
        "business_status": business,
        "succeeded": succeeded,
        "sources": [
            {"source": "tankan", "status": business},
            {"source": "lutou", "status": business},
            {"source": "lutou_domestic_basis", "status": business},
        ],
        "consumer_freshness_validation": {"status": "PASS", "results": []},
        "production_data_package": {"status": "GENERATED", "package_id": "public-current-abc"},
        "server_sync": "SYNCED",
        "manifest": "PASS",
        "sha": "PASS",
        "atomic_current_switch": "PASS",
        "formal_read_validation": "PASS",
        "prewarm": {"status": prewarm, "targets": {}},
    }


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def _commit(repository: Path, filename: str, content: str, message: str) -> str:
    (repository / filename).write_text(content, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _formal_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "formal"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    production = _commit(repository, "production.txt", "approved\n", "production")
    _git(repository, "update-ref", "refs/remotes/origin/main", production)
    return repository, production, _git(repository, "rev-parse", f"{production}^{{tree}}")


def test_default_automation_runtime_is_local_app_data() -> None:
    root = wrapper.default_runtime_root(
        {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}
    )
    assert root == Path(
        r"C:\Users\tester\AppData\Local\market-data-runtime\automation"
    )
    assert "Desktop" not in root.parts


def test_local_runtime_filesystem_passes_and_is_created(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime = tmp_path / "local-app-data" / "market-data-runtime" / "automation"
    evidence = wrapper.validate_runtime_filesystem(runtime, repository)
    assert runtime.is_dir()
    assert evidence["cloud_files"] is False
    assert evidence["runtime_class"] == "LOCALAPPDATA_LOCAL_FILESYSTEM"


def test_cloud_files_reparse_runtime_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(
        wrapper,
        "_reparse_tag",
        lambda path: 0x9000601A if path == runtime.absolute() else None,
    )
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_runtime_filesystem(runtime, repository)
    assert caught.value.stage == "RUNTIME_FILESYSTEM"
    assert "Cloud Files" in caught.value.safe_reason


def test_runtime_inside_repository_is_rejected_without_deleting_history(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    history = repository / "old-runtime" / "historical-run.json"
    history.parent.mkdir()
    history.write_text("evidence", encoding="utf-8")
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_runtime_filesystem(repository / "runtime", repository)
    assert caught.value.stage == "RUNTIME_FILESYSTEM"
    assert history.read_text(encoding="utf-8") == "evidence"


@pytest.mark.parametrize("business", ["UPDATED", "NO_CHANGE"])
def test_valid_updated_and_no_change_are_success(tmp_path: Path, business: str) -> None:
    run_id = "full-daily-20260831T010203.000001Z-abcdef12"
    path = _write(tmp_path / "manifest.json", _manifest(run_id, business=business))
    result = wrapper.validate_daily_manifest(path, run_id, 0)
    assert result["manifest"]["business_status"] == business
    assert result["warnings"] == []
    invocation = wrapper.Invocation(
        run_id, "manual", "2026-08-31T01:02:03Z",
        "a" * 40, "b" * 40, "c" * 40, "d" * 40, "main", "python.exe",
    )
    final = wrapper.make_final_status(invocation, status="SUCCESS", completed_at="2026-08-31T01:03:03Z", failed_stage=None, safe_reason="PASS", process_exit_code=0, manifest_path=path, manifest=result["manifest"])
    assert final["status"] == "SUCCESS"
    assert final["daily_manifest_sha256"] == wrapper.sha256_file(path)
    assert final["repository_main_head"] == "a" * 40
    assert final["repository_main_tree"] == "b" * 40
    assert final["production_commit"] == "c" * 40
    assert final["production_tree"] == "d" * 40


def test_source_unavailable_with_zero_exit_is_failed(tmp_path: Path) -> None:
    run_id = "run-source-unavailable"
    value = _manifest(run_id, business="SOURCE_UNAVAILABLE", succeeded=True)
    value["sources"][0]["status"] = "SOURCE_UNAVAILABLE"  # type: ignore[index]
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_daily_manifest(_write(tmp_path / "manifest.json", value), run_id, 0)
    assert caught.value.stage == "MANIFEST_VALIDATION"


def test_nonzero_process_exit_always_fails_before_manifest(tmp_path: Path) -> None:
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_daily_manifest(tmp_path / "missing.json", "run", 9)
    assert caught.value.stage == "ENTRYPOINT_EXCEPTION"


def test_nonzero_process_exit_uses_manifest_for_precise_stage(tmp_path: Path) -> None:
    value = _manifest("run")
    value["succeeded"] = False
    value["business_status"] = "FAILED"
    value["delivery_artifact_producer"] = "FAIL"
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_daily_manifest(_write(tmp_path / "manifest.json", value), "run", 1)
    assert caught.value.stage == "DOMESTIC_SPREAD"


@pytest.mark.parametrize("case", ["missing", "run_id", "schema"])
def test_missing_mismatched_or_invalid_manifest_fails(tmp_path: Path, case: str) -> None:
    run_id = "expected-run"
    path = tmp_path / "manifest.json"
    if case != "missing":
        value = _manifest(run_id)
        if case == "run_id":
            value["run_id"] = "another-run"
        else:
            value["schema_version"] = "unknown/9"
        _write(path, value)
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_daily_manifest(path, run_id, 0)
    assert caught.value.stage == "MANIFEST_VALIDATION"


@pytest.mark.parametrize("prewarm", ["SKIPPED", "PARTIAL", "FAIL"])
def test_prewarm_nonpass_is_success_warning_after_data_contract_passes(tmp_path: Path, prewarm: str) -> None:
    run_id = f"run-{prewarm.lower()}"
    result = wrapper.validate_daily_manifest(_write(tmp_path / "manifest.json", _manifest(run_id, prewarm=prewarm)), run_id, 0)
    assert result["warnings"] == [f"PREWARM_{prewarm}"]


def test_failed_server_formal_read_cannot_be_hidden_by_prewarm(tmp_path: Path) -> None:
    value = _manifest("run", prewarm="PASS")
    value["formal_read_validation"] = "FAIL"
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.validate_daily_manifest(_write(tmp_path / "manifest.json", value), "run", 0)
    assert caught.value.stage == "FORMAL_READ"


def test_lock_conflict_skips_both_trigger_types_and_release_allows_later_run(tmp_path: Path) -> None:
    lock_path, active_path = tmp_path / "full-daily.lock", tmp_path / "active-run.json"
    with wrapper.lifecycle_lock(lock_path, active_path, "first"):
        for trigger in ("manual", "scheduled"):
            with pytest.raises(wrapper.WrapperFailure) as caught:
                with wrapper.lifecycle_lock(lock_path, active_path, trigger):
                    pytest.fail("conflicting run entered lifecycle")
            assert caught.value.stage == "LOCK"
            assert json.loads(active_path.read_text(encoding="utf-8"))["active_run_id"] == "first"
    with wrapper.lifecycle_lock(lock_path, active_path, "same-day-second"):
        assert json.loads(active_path.read_text(encoding="utf-8"))["active_run_id"] == "same-day-second"


@pytest.mark.parametrize("trigger", ["manual", "scheduled"])
def test_lock_conflict_run_writes_machine_readable_skip_without_workspace(tmp_path: Path, trigger: str) -> None:
    automation = tmp_path / "automation"
    with wrapper.lifecycle_lock(automation / "full-daily.lock", automation / "active-run.json", "active"):
        assert wrapper.run_wrapper(trigger, repository=tmp_path / "unused", automation_root=automation) == 3
    statuses = list((automation / "runs").glob("*/final-status.json"))
    assert len(statuses) == 1
    final = json.loads(statuses[0].read_text(encoding="utf-8"))
    assert final["status"] == "SKIPPED_ALREADY_RUNNING"
    assert final["failed_stage"] == "LOCK"
    assert "active_run_id=active" in final["safe_reason"]
    assert not (statuses[0].parent / "runtime").exists()
    assert not (statuses[0].parent / "tool-repo").exists()


def test_preflight_repository_hard_fail_never_calls_full_daily(tmp_path: Path) -> None:
    automation = tmp_path / "automation"
    assert wrapper.run_wrapper("manual", repository=tmp_path / "not-a-repo", automation_root=automation) == 1
    run_dir = next((automation / "runs").iterdir())
    final = json.loads((run_dir / "final-status.json").read_text(encoding="utf-8"))
    assert final["failed_stage"] == "REPOSITORY"
    preflight = json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight["gates"][-1]["status"] == "FAIL"
    assert not (run_dir / "run.log").exists()


def test_baseline_download_is_strict_noninteractive_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    def complete(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(wrapper.subprocess, "run", complete)
    wrapper.download_runtime_baseline("trusted-host", "/safe/store", "public-current-abc", tmp_path / "baseline", timeout=42)
    command = seen["command"]
    assert Path(command[0]).name.lower() in {"scp", "scp.exe"}
    joined = " ".join(command)
    assert "BatchMode=yes" in joined and "StrictHostKeyChecking=yes" in joined
    assert "ConnectTimeout=15" in joined
    assert seen["timeout"] == 42


def test_baseline_download_timeout_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wrapper.subprocess, "run", lambda command, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, kwargs["timeout"])))
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.download_runtime_baseline("trusted-host", "/safe/store", "public-current-abc", tmp_path / "baseline")
    assert caught.value.stage == "TIMEOUT"


def test_runtime_baseline_identity_mismatch_stops_refresh() -> None:
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.require_baseline_matches({"package_id": "public-current-old"}, {"package_id": "public-current-live"})
    assert caught.value.stage == "RUNTIME_BASELINE"


def test_provider_preflight_is_read_only_and_requires_every_source_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    payload = {"sources": [{"source": "tankan", "status": "READY"}, {"source": "lutou", "status": "READY"}, {"source": "lutou_domestic_basis", "status": "READY"}]}
    def complete(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
    monkeypatch.setattr(wrapper.subprocess, "run", complete)
    result = wrapper.run_provider_preflight(Path("python.exe"), tmp_path, tmp_path / "runtime", "run", tmp_path / "t.env", tmp_path / "l.env")
    assert result["sources"] == payload["sources"]
    assert "--dry-run" in seen["command"]
    assert seen["env"]["PYTHONUTF8"] == "1"


def test_provider_preflight_hard_failure_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"sources": [{"source": "tankan", "status": "SOURCE_UNAVAILABLE"}]}
    monkeypatch.setattr(wrapper.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(payload), ""))
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.run_provider_preflight(Path("python.exe"), tmp_path, tmp_path / "runtime", "run", tmp_path / "t.env", tmp_path / "l.env")
    assert caught.value.stage == "PROVIDER_PREFLIGHT"


def test_manifest_failure_prefers_structured_weather_root_over_legacy_reason() -> None:
    manifest = {
        "root_failure": {
            "provider": "lutou", "domain": "weather", "stage": "SOIL_EVIDENCE",
            "exception_type": "WeatherError", "underlying_exception_type": "ValueError",
            "safe_message": "safe",
        },
        "transaction": {"rollback": "PASS"},
        "safe_reason": "provider preflight failed",
        "sources": [{"source": "lutou", "read": "READY", "status": "INGESTION_FAILURE"}],
    }
    assert wrapper.classify_manifest_failure(manifest) == "WEATHER"


def test_manifest_failure_preserves_precise_weather_cleanup_stage() -> None:
    manifest = {
        "root_failure": {
            "provider": "lutou",
            "domain": "weather",
            "stage": "WEATHER_TABLE_PARTITION_CLEANUP",
            "exception_type": "LutouWeatherStageError",
            "underlying_exception_type": "OSError",
            "safe_message": "WinError 145; operation=shutil.rmtree",
        },
        "transaction": {"rollback": "PASS"},
    }
    assert (
        wrapper.classify_manifest_failure(manifest)
        == "WEATHER_TABLE_PARTITION_CLEANUP"
    )


def test_manifest_failure_reports_rollback_after_unmapped_root() -> None:
    manifest = {
        "root_failure": {
            "provider": "lutou", "domain": None, "stage": "UNKNOWN",
            "exception_type": "RuntimeError", "underlying_exception_type": None,
            "safe_message": "safe",
        },
        "transaction": {"rollback": "FAIL"},
    }
    assert wrapper.classify_manifest_failure(manifest) == "ROLLBACK"


def test_manifest_failure_only_uses_provider_preflight_for_readiness_failure() -> None:
    readiness = {
        "sources": [
            {"source": "lutou", "read": "SOURCE_UNAVAILABLE", "status": "SOURCE_UNAVAILABLE"}
        ]
    }
    post_read = {
        "sources": [
            {"source": "lutou", "read": "READY", "status": "INGESTION_FAILURE", "safe_reason": "unknown"}
        ]
    }
    assert wrapper.classify_manifest_failure(readiness) == "PROVIDER_PREFLIGHT"
    assert wrapper.classify_manifest_failure(post_read) == "ENTRYPOINT_EXCEPTION"


def test_legacy_manifest_weather_keyword_remains_compatible() -> None:
    manifest = {
        "sources": [
            {"source": "lutou", "read": "READY", "status": "INGESTION_FAILURE", "safe_reason": "Lutou Weather failed"}
        ]
    }
    assert wrapper.classify_manifest_failure(manifest) == "WEATHER"


@pytest.mark.skipif(os.name != "nt", reason="Windows crash-release contract")
def test_os_releases_lock_when_owner_process_crashes(tmp_path: Path) -> None:
    lock_path = tmp_path / "crash.lock"
    code = "import os,sys; from filelock import FileLock; lock=FileLock(sys.argv[1]); lock.acquire(); print('LOCKED',flush=True); os._exit(7)"
    child = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdout=subprocess.PIPE, text=True)
    assert child.stdout and child.stdout.readline().strip() == "LOCKED"
    assert child.wait(timeout=10) == 7
    lock = FileLock(lock_path, timeout=1)
    with lock:
        assert lock.is_locked


def test_utf8_atomic_status_round_trip_under_chinese_path(tmp_path: Path) -> None:
    path = tmp_path / "中文目录" / "最终状态.json"
    wrapper.atomic_write_json(path, {"safe_reason": "网络预检通过；日志完整"})
    assert json.loads(path.read_text(encoding="utf-8"))["safe_reason"] == "网络预检通过；日志完整"
    assert not list(path.parent.glob("*.tmp"))


def test_child_environment_forces_utf8_and_never_uses_partial_lutou_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUTOU_HOST", "partial-only")
    env = wrapper.runtime_environment()
    assert "LUTOU_HOST" not in env
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_manual_and_scheduled_have_identical_business_command() -> None:
    arguments = (Path("python.exe"), Path("tool"), Path("runtime"), "run", "host", "/store", f"sha256:{'a' * 64}")
    manual = wrapper.business_command(*arguments)
    scheduled = wrapper.business_command(*arguments)
    assert manual == scheduled
    assert manual[1].endswith(str(Path("04_scripts") / "refresh_public_data.py"))


def test_current_main_equal_to_production_commit_is_allowed(tmp_path: Path) -> None:
    repository, production, production_tree = _formal_repository(tmp_path)
    identity = wrapper.repository_identity(repository, production)
    assert identity.repository_main_head == production
    assert identity.repository_main_tree == production_tree
    assert identity.production_commit == production
    assert identity.production_tree == production_tree


@pytest.mark.parametrize("trigger", ["manual", "scheduled"])
def test_main_can_advance_while_both_triggers_keep_approved_production(
    tmp_path: Path, trigger: str,
) -> None:
    repository, production, production_tree = _formal_repository(tmp_path)
    main_head = _commit(repository, "frontend-only-change.txt", "UI only\n", "frontend only")
    _git(repository, "update-ref", "refs/remotes/origin/main", main_head)

    identity = wrapper.repository_identity(repository, production)
    invocation = wrapper.Invocation(
        "run", trigger, "2026-08-31T01:02:03Z",
        identity.repository_main_head, identity.repository_main_tree,
        identity.production_commit, identity.production_tree,
        identity.repository_branch, "python.exe",
    )
    evidence = {**invocation.__dict__, "schema_version": wrapper.SCHEMA_VERSION}
    assert evidence["repository_main_head"] == main_head
    assert evidence["production_commit"] == production

    tool_repo = tmp_path / f"tool-{trigger}"
    wrapper.create_trusted_tool_repo(repository, tool_repo, production, production_tree)
    assert _git(tool_repo, "rev-parse", "HEAD") == production
    assert _git(tool_repo, "rev-parse", "HEAD^{tree}") == production_tree
    assert _git(tool_repo, "branch", "--show-current") == ""
    assert _git(tool_repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not (tool_repo / "frontend-only-change.txt").exists()


@pytest.mark.parametrize("production", ["", "abc123", "A" * 40])
def test_production_commit_must_be_full_lowercase_sha(tmp_path: Path, production: str) -> None:
    repository, _, _ = _formal_repository(tmp_path)
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.repository_identity(repository, production)
    assert caught.value.stage == "REPOSITORY"


def test_nonexistent_production_commit_is_rejected(tmp_path: Path) -> None:
    repository, _, _ = _formal_repository(tmp_path)
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.repository_identity(repository, "f" * 40)
    assert caught.value.stage == "REPOSITORY"
    assert "known local commit" in caught.value.safe_reason


def test_feature_only_and_unpushed_local_commit_are_rejected(tmp_path: Path) -> None:
    repository, production, _ = _formal_repository(tmp_path)
    _git(repository, "checkout", "-b", "feature")
    feature = _commit(repository, "feature.txt", "not approved\n", "feature only")
    _git(repository, "checkout", "main")
    assert _git(repository, "rev-parse", "origin/main") == production
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.repository_identity(repository, feature)
    assert caught.value.stage == "REPOSITORY"
    assert "not an ancestor" in caught.value.safe_reason


def test_dirty_workspace_is_still_rejected(tmp_path: Path) -> None:
    repository, production, _ = _formal_repository(tmp_path)
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.repository_identity(repository, production)
    assert caught.value.stage == "REPOSITORY"


def test_local_main_divergence_from_origin_main_is_still_rejected(tmp_path: Path) -> None:
    repository, production, _ = _formal_repository(tmp_path)
    _commit(repository, "local-only.txt", "unpushed\n", "local only")
    with pytest.raises(wrapper.WrapperFailure) as caught:
        wrapper.repository_identity(repository, production)
    assert caught.value.stage == "REPOSITORY"


def test_trusted_tool_repo_is_exact_head_tree_and_clean(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "中文.txt").write_text("可信代码", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "seed"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip()
    destination = tmp_path / "可信工作区"
    wrapper.create_trusted_tool_repo(source, destination, head, tree)
    assert (destination / "中文.txt").read_text(encoding="utf-8") == "可信代码"
    assert subprocess.check_output(["git", "-C", str(destination), "status", "--porcelain"], text=True) == ""


def test_trusted_tool_repo_enables_windows_long_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def complete(command: list[str], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    identities = iter(("head", "tree", ""))
    monkeypatch.setattr(wrapper.subprocess, "run", complete)
    monkeypatch.setattr(wrapper, "_git", lambda *_args: next(identities))
    monkeypatch.setattr(wrapper, "tool_path", lambda name: f"{name}.exe")

    wrapper.create_trusted_tool_repo(
        tmp_path / "source", tmp_path / "tool-repo", "head", "tree"
    )

    clone = calls[0]
    assert clone[:5] == ["git.exe", "clone", "-c", "core.longpaths=true", "--local"]


def test_run_id_is_sortable_unique_and_allows_same_day_multiple_runs() -> None:
    now = datetime(2026, 8, 31, 1, 2, 3, 456789, tzinfo=timezone.utc)
    first, second = wrapper.new_run_id(now), wrapper.new_run_id(now)
    assert first != second
    assert first.startswith("full-daily-20260831T010203.456789Z-")

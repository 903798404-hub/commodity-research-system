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


@pytest.mark.parametrize("business", ["UPDATED", "NO_CHANGE"])
def test_valid_updated_and_no_change_are_success(tmp_path: Path, business: str) -> None:
    run_id = "full-daily-20260831T010203.000001Z-abcdef12"
    path = _write(tmp_path / "manifest.json", _manifest(run_id, business=business))
    result = wrapper.validate_daily_manifest(path, run_id, 0)
    assert result["manifest"]["business_status"] == business
    assert result["warnings"] == []
    invocation = wrapper.Invocation(run_id, "manual", "2026-08-31T01:02:03Z", "a" * 40, "b" * 40, "main", "python.exe")
    final = wrapper.make_final_status(invocation, status="SUCCESS", completed_at="2026-08-31T01:03:03Z", failed_stage=None, safe_reason="PASS", process_exit_code=0, manifest_path=path, manifest=result["manifest"])
    assert final["status"] == "SUCCESS"
    assert final["daily_manifest_sha256"] == wrapper.sha256_file(path)


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


def test_run_id_is_sortable_unique_and_allows_same_day_multiple_runs() -> None:
    now = datetime(2026, 8, 31, 1, 2, 3, 456789, tzinfo=timezone.utc)
    first, second = wrapper.new_run_id(now), wrapper.new_run_id(now)
    assert first != second
    assert first.startswith("full-daily-20260831T010203.456789Z-")

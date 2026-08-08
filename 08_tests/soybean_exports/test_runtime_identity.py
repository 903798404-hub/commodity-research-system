from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agri_research_agent.soybean_exports import common
from agri_research_agent.soybean_exports.common import PipelineError, resolve_runtime_git_head


REPOSITORY = Path(__file__).resolve().parents[2]
FGIS_CLI = REPOSITORY / "04_scripts/soybean_exports/run_fgis_export_inspections.py"
FAS_CLI = REPOSITORY / "04_scripts/soybean_exports/run_fas_export_sales.py"
GIT_HEAD = "a" * 40
OTHER_GIT_HEAD = "b" * 40
GIT_TREE = "c" * 40


def write_release(path: Path, *, git_commit: str = GIT_HEAD, **changes: Any) -> None:
    payload = {
        "application": "spread-dashboard",
        "release_id": "spread-20260808-aaaaaaaaaaaa-b01",
        "git_commit": git_commit,
        "git_tree": GIT_TREE,
        "build_time": "2026-08-08T00:00:00Z",
        "source": "https://github.com/example/commodity-research-system",
        **changes,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def no_git(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise AssertionError("production identity resolution must not call git")


def test_production_env_and_release_resolve_without_git(tmp_path: Path) -> None:
    release = tmp_path / "RELEASE.json"
    write_release(release, git_commit=GIT_HEAD.upper())
    assert resolve_runtime_git_head(
        project_root=tmp_path,
        environment={"MARKET_DATA_GIT_HEAD": GIT_HEAD},
        release_path=release,
        git_runner=no_git,
    ) == GIT_HEAD


def test_production_env_and_release_mismatch_fails_without_git(tmp_path: Path) -> None:
    release = tmp_path / "RELEASE.json"
    write_release(release, git_commit=OTHER_GIT_HEAD)
    with pytest.raises(PipelineError, match="disagree"):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment={"MARKET_DATA_GIT_HEAD": GIT_HEAD},
            release_path=release,
            git_runner=no_git,
        )


@pytest.mark.parametrize(
    ("environment", "create_release", "message"),
    [
        ({}, True, "missing MARKET_DATA_GIT_HEAD"),
        ({"MARKET_DATA_GIT_HEAD": GIT_HEAD}, False, "missing /app/RELEASE.json"),
    ],
)
def test_partial_production_identity_fails_without_git(
    tmp_path: Path,
    environment: dict[str, str],
    create_release: bool,
    message: str,
) -> None:
    release = tmp_path / "RELEASE.json"
    if create_release:
        write_release(release)
    with pytest.raises(PipelineError, match=message):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment=environment,
            release_path=release,
            git_runner=no_git,
        )


@pytest.mark.parametrize("invalid", ["", "a" * 39, "g" * 40])
def test_invalid_environment_git_head_fails_without_git(
    tmp_path: Path, invalid: str
) -> None:
    release = tmp_path / "RELEASE.json"
    write_release(release)
    with pytest.raises(PipelineError, match="40-character"):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment={"MARKET_DATA_GIT_HEAD": invalid},
            release_path=release,
            git_runner=no_git,
        )


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("{", "unreadable or invalid"),
        (json.dumps({"git_commit": GIT_HEAD}), "schema is invalid"),
        (
            json.dumps(
                {
                    "application": "spread-dashboard",
                    "release_id": "release",
                    "git_commit": "a" * 39,
                    "git_tree": GIT_TREE,
                    "build_time": "time",
                    "source": "source",
                }
            ),
            "40-character",
        ),
    ],
)
def test_invalid_release_fails_without_git(
    tmp_path: Path, contents: str, message: str
) -> None:
    release = tmp_path / "RELEASE.json"
    release.write_text(contents, encoding="utf-8")
    with pytest.raises(PipelineError, match=message):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment={"MARKET_DATA_GIT_HEAD": GIT_HEAD},
            release_path=release,
            git_runner=no_git,
        )


def test_local_checkout_falls_back_to_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    calls: list[tuple[list[str], Path]] = []

    def local_git(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["cwd"]))
        return subprocess.CompletedProcess(command, 0, GIT_HEAD.upper() + "\n", "")

    assert resolve_runtime_git_head(
        project_root=tmp_path,
        environment={},
        release_path=tmp_path / "missing-release.json",
        git_runner=local_git,
    ) == GIT_HEAD
    assert calls == [(["git", "rev-parse", "HEAD"], tmp_path)]


def test_local_checkout_without_git_fails_clearly(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    def missing_git(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    with pytest.raises(PipelineError, match="local development.*git was not found"):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment={},
            release_path=tmp_path / "missing-release.json",
            git_runner=missing_git,
        )


def test_missing_production_identities_do_not_fall_back_without_local_checkout(
    tmp_path: Path,
) -> None:
    with pytest.raises(PipelineError, match="production identities are absent"):
        resolve_runtime_git_head(
            project_root=tmp_path,
            environment={},
            release_path=tmp_path / "missing-release.json",
            git_runner=no_git,
        )


@pytest.mark.parametrize(
    ("script", "pipeline_name"),
    [(FGIS_CLI, "run_fgis_pipeline"), (FAS_CLI, "run_fas_pipeline")],
)
def test_export_clis_use_shared_runtime_identity_without_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Path,
    pipeline_name: str,
) -> None:
    release = tmp_path / "RELEASE.json"
    write_release(release)
    monkeypatch.setenv("MARKET_DATA_GIT_HEAD", GIT_HEAD)
    monkeypatch.setattr(common, "RUNTIME_RELEASE_PATH", release)
    monkeypatch.setattr(common.subprocess, "run", no_git)
    namespace = runpy.run_path(str(script))
    script_globals = namespace["main"].__globals__
    observed: dict[str, Any] = {}

    class Adapter:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    def pipeline(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"status": "candidate_only"}

    if pipeline_name == "run_fgis_pipeline":
        script_globals["FgisYearlyAdapter"] = Adapter
        argv = [str(script), "--runtime-root", str(tmp_path / "runtime"), "--candidate-only"]
    else:
        script_globals["resolve_fas_api_key"] = lambda *_args, **_kwargs: (
            "test-key",
            "test",
        )
        script_globals["FasAdapter"] = Adapter
        argv = [str(script), "--runtime-root", str(tmp_path / "runtime"), "--candidate-only"]
    script_globals[pipeline_name] = pipeline
    monkeypatch.setattr(sys, "argv", argv)

    assert namespace["main"]() == 0
    assert observed["git_head"] == GIT_HEAD


def test_export_clis_do_not_duplicate_git_identity_logic() -> None:
    for script in (FGIS_CLI, FAS_CLI):
        source = script.read_text(encoding="utf-8")
        assert "resolve_runtime_git_head" in source
        assert "subprocess" not in source
        assert '["git", "rev-parse", "HEAD"]' not in source

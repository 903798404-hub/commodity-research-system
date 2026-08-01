from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_TOOL = REPO_ROOT / "04_scripts" / "environment"
if str(ENVIRONMENT_TOOL) not in sys.path:
    sys.path.insert(0, str(ENVIRONMENT_TOOL))

import verify_development_environment as environment  # noqa: E402


def test_python_contract_is_fixed_to_python_312_and_lock_inputs_are_synced() -> None:
    checks = environment.check_python_contract(REPO_ROOT)
    assert all(check.status == "PASS" for check in checks), checks
    assert 'requires-python = ">=3.12,<3.13"' in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_direct_pin_parser_accepts_only_fixed_linux_platform_markers(tmp_path: Path) -> None:
    valid = tmp_path / "valid.in"
    valid.write_text(
        'mini-racer==0.14.1 ; platform_system != "Linux"\n'
        'py-mini-racer==0.6.0 ; platform_system == "Linux"\n',
        encoding="utf-8",
    )
    assert environment._pinned_requirements(valid) == {
        "mini-racer": "0.14.1",
        "py-mini-racer": "0.6.0",
    }

    invalid = tmp_path / "invalid.in"
    invalid.write_text('example>=1 ; python_version > "3.10"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="non-pinned direct dependency"):
        environment._pinned_requirements(invalid)


def test_python_314_is_not_accepted_as_the_formal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment.sys, "version_info", (3, 14, 0, "final", 0))
    checks = environment.check_python(REPO_ROOT, allow_candidate=False)
    assert checks[0].status == "FAIL"
    assert "Python 3.14" in checks[0].advice


def test_frontend_contracts_pin_node_pnpm_and_frozen_lockfiles() -> None:
    checks = environment.check_frontend_contract(REPO_ROOT)
    assert all(check.status == "PASS" for check in checks), checks
    for frontend in environment.FRONTENDS:
        package = (REPO_ROOT / frontend / "package.json").read_text(encoding="utf-8")
        assert '"packageManager": "pnpm@10.12.1"' in package
        assert '"node": ">=24 <25"' in package


def test_canonical_dockerfiles_use_locked_dependencies_and_frozen_frontend_installs() -> None:
    spread_dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in spread_dockerfile
    assert "--require-hashes -r requirements.txt" in spread_dockerfile
    for dockerfile in (
        REPO_ROOT / "11_独立应用" / "USDA平衡表" / "Dockerfile",
        REPO_ROOT / "11_独立应用" / "OilWorld平衡表" / "Dockerfile",
    ):
        contents = dockerfile.read_text(encoding="utf-8")
        assert "FROM node:24-alpine AS build" in contents
        assert "corepack enable" in contents
        assert "pnpm install --frozen-lockfile" in contents


def test_preflight_rejects_a_codex_fallback_pnpm(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = {"node": r"C:\\Program Files\\nodejs\\node.exe", "corepack": r"C:\\Program Files\\nodejs\\corepack.cmd", "pnpm": r"C:\\Users\\codex\\.cache\\codex-runtimes\\fallback\\pnpm.cmd"}
    monkeypatch.setattr(environment.shutil, "which", lambda name: paths[name])
    monkeypatch.setattr(environment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="v24.0.0", stderr=""))
    checks = environment.check_local_node_tools()
    assert any(check.subject == "pnpm executable" and check.status == "FAIL" for check in checks)

"""The formal isolated entry imports only after verifying its control clone."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = Path("04_scripts/automation/run_production_data_delta_windows.py")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def isolated_control(tmp_path: Path) -> tuple[Path, Path, Path]:
    control = tmp_path / "verified-control"
    (control / ENTRY.parent).mkdir(parents=True)
    (control / ENTRY).write_bytes((ROOT / ENTRY).read_bytes())
    source = control / "03_src/agri_research_agent/automation"
    source.mkdir(parents=True)
    (source.parent / "__init__.py").write_text("", encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "sentinel.py").write_text("SOURCE = 'verified-control'\n", encoding="utf-8")
    (source / "production_data_delta.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "def validate_config(config):\n"
        "    pass\n"
        "def verify_clean_detached_clone(root, config):\n"
        "    assert str(root / '03_src') not in sys.path\n"
        "def _loaded():\n"
        "    from agri_research_agent.automation import sentinel\n"
        "    return sentinel.SOURCE\n"
        "def promote_existing_candidate(config, **kwargs):\n"
        "    return {'status': 'PUBLISHED', 'loaded': _loaded()}\n"
        "def run_domain(config, domain, **kwargs):\n"
        "    return {'status': 'CANDIDATE', 'loaded': _loaded()}\n",
        encoding="utf-8",
    )
    _git(control, "init", "-q")
    _git(control, "config", "--local", "core.autocrlf", "false")
    _git(control, "remote", "add", "origin", "https://example.invalid/verified-control.git")
    _git(control, "add", "--all")
    _git(control, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Verified control fixture")
    _git(control, "checkout", "--detach", "HEAD")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "approved_commit": _git(control, "rev-parse", "HEAD"),
        "approved_tree": _git(control, "rev-parse", "HEAD^{tree}"),
        "origin": "https://example.invalid/verified-control.git",
    }), encoding="utf-8")
    external = tmp_path / "untrusted-caller"
    malicious = external / "agri_research_agent/automation"
    malicious.mkdir(parents=True)
    (malicious.parent / "__init__.py").write_text("", encoding="utf-8")
    (malicious / "__init__.py").write_text("", encoding="utf-8")
    (malicious / "sentinel.py").write_text("SOURCE = 'untrusted-caller'\n", encoding="utf-8")
    return control, config, external


def _invoke(control: Path, config: Path, external: Path, *args: str,
            domain: str = "akshare") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(external)
    return subprocess.run(
        [sys.executable, "-I", "-B", str(control / ENTRY), "--config", str(config),
         "--domain", domain, *args], cwd=external, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )


def test_promotion_imports_verified_control_without_external_pythonpath(isolated_control):
    control, config, external = isolated_control
    result = _invoke(
        control, config, external,
        "--historical-reconciliation-manifest", str(external / "reconciliation.json"),
        "--promote-candidate", str(external / "candidate"),
        "--promotion-evidence", str(external / "evidence.json"),
        "--promotion-evidence-sha256", "a" * 64,
        "--expected-current-id", "public-current-" + "b" * 24,
        "--expected-current-artifact-sha256", "c" * 64,
        "--expected-current-manifest-sha256", "d" * 64,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["loaded"] == "verified-control"


def test_normal_branch_keeps_verified_control_import(isolated_control):
    control, config, external = isolated_control
    result = _invoke(control, config, external, domain="soybean_crop_progress")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["loaded"] == "verified-control"


def test_unverified_control_fails_before_import(isolated_control):
    control, config, external = isolated_control
    value = json.loads(config.read_text(encoding="utf-8"))
    value["approved_commit"] = "0" * 40
    config.write_text(json.dumps(value), encoding="utf-8")
    result = _invoke(control, config, external)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "PRODUCTION_DATA_DELTA": "FAIL", "reason": "ValueError",
    }

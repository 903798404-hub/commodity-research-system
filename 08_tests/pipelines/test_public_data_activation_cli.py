from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agri_research_agent.pipelines import public_data_prewarm

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "04_scripts" / "activate_public_data_package.py"
SPEC = importlib.util.spec_from_file_location("public_data_activation_test", SCRIPT)
assert SPEC and SPEC.loader
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


def test_runtime_root_is_scoped_to_each_formal_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "PUBLIC_MARKET_DATA_RUNTIME_ROOT"
    monkeypatch.setenv(name, "existing-runtime")
    observed: list[str | None] = []
    loader = public_data_prewarm._runtime_scoped_loader(
        tmp_path,
        lambda: observed.append(os.environ.get(name)),
    )

    loader()

    assert observed == [str(tmp_path.resolve())]
    assert os.environ[name] == "existing-runtime"


def test_validate_only_reads_sealed_package_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = SimpleNamespace(
        package_id="public-current-abc",
        directory=tmp_path / "incoming" / "public-current-abc.upload-one",
        manifest={"delivery_artifacts": {}},
    )
    package.directory.mkdir(parents=True)
    (package.directory / "manifest.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        activation,
        "validate_production_package",
        lambda path, require_directory_name=False: (
            calls.append(f"package:{Path(path).name}:{require_directory_name}") or package
        ),
    )
    monkeypatch.setattr(
        activation,
        "validate_formal_consumer_reads",
        lambda **kwargs: (
            calls.append(f"formal:{Path(kwargs['runtime_root']).name}")
            or SimpleNamespace(targets={"consumer": "PASS"})
        ),
    )
    monkeypatch.setattr(
        activation,
        "activate_incoming_server_package",
        lambda *args, **kwargs: pytest.fail("validate-only must not activate"),
    )

    assert activation.main([
        "--incoming-package", str(package.directory),
        "--store-root", str(tmp_path / "store"),
        "--validate-only",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "VALIDATED"
    assert payload["consumer_reads"] == {"consumer": "PASS"}
    assert payload["activation_capabilities"] == [
        "public-current-server-cas/1",
        "application-runtime-readability-rollback/1",
    ]
    assert payload["manifest_sha256"] == activation.hashlib.sha256(b"{}").hexdigest()
    assert calls == ["package:public-current-abc.upload-one:False", "formal:data"]


def test_validate_only_fails_closed_on_consumer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path)
    monkeypatch.setattr(
        activation, "validate_production_package", lambda *args, **kwargs: package
    )
    monkeypatch.setattr(
        activation,
        "validate_formal_consumer_reads",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("formal consumer validation failed: consumer=FAIL:ValueError")
        ),
    )

    with pytest.raises(RuntimeError, match="formal consumer validation failed"):
        activation.main([
            "--incoming-package", str(tmp_path),
            "--store-root", str(tmp_path / "store"),
            "--validate-only",
        ])


def test_application_runtime_failure_rollback_cli_uses_formal_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    def rollback(**kwargs):
        observed.update(kwargs)
        kwargs["post_rollback_validator"](tmp_path / "old" / "data")
        return SimpleNamespace(
            status="ROLLED_BACK",
            failed_package_id="public-current-new",
            restored_package_id="public-current-old",
            atomic_switch="PASS",
            formal_read_validation="PASS",
            safe_reason=None,
        )

    formal: list[Path] = []
    monkeypatch.setattr(
        activation, "rollback_server_current_after_application_failure", rollback
    )
    monkeypatch.setattr(
        activation, "validate_formal_consumer_reads",
        lambda **kwargs: formal.append(Path(kwargs["runtime_root"])),
    )
    assert activation.main([
        "--store-root", str(tmp_path / "store"),
        "--rollback-after-application-readability-failure",
        "--expected-failed-current-id", "public-current-new",
        "--rollback-current-id", "public-current-old",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ROLLED_BACK"
    assert observed["expected_failed_package_id"] == "public-current-new"
    assert observed["rollback_package_id"] == "public-current-old"
    assert formal == [tmp_path / "old" / "data"]

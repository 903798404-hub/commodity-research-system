from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agri_research_agent.pipelines.public_data_delivery import PrewarmStatus


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "04_scripts" / "activate_public_data_package.py"
SPEC = importlib.util.spec_from_file_location("public_data_activation_test", SCRIPT)
assert SPEC and SPEC.loader
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


def test_validate_only_reads_sealed_package_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = SimpleNamespace(
        package_id="public-current-abc",
        directory=tmp_path / "incoming" / "public-current-abc.upload-one",
    )
    package.directory.mkdir(parents=True)
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
        "validate_activated_public_currents",
        lambda path: calls.append(f"current:{Path(path).name}"),
    )
    monkeypatch.setattr(
        activation,
        "build_consumer_prewarm_targets",
        lambda **kwargs: (SimpleNamespace(name="consumer"),),
    )
    monkeypatch.setattr(
        activation,
        "run_prewarm",
        lambda targets: SimpleNamespace(
            status=PrewarmStatus.PASS, targets={"consumer": "PASS"}
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
    assert calls == ["package:public-current-abc.upload-one:False", "current:data"]


def test_validate_only_fails_closed_on_consumer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = SimpleNamespace(package_id="public-current-abc", directory=tmp_path)
    monkeypatch.setattr(
        activation, "validate_production_package", lambda *args, **kwargs: package
    )
    monkeypatch.setattr(activation, "validate_activated_public_currents", lambda _path: None)
    monkeypatch.setattr(activation, "build_consumer_prewarm_targets", lambda **kwargs: ())
    monkeypatch.setattr(
        activation,
        "run_prewarm",
        lambda _targets: SimpleNamespace(
            status=PrewarmStatus.FAIL, targets={"consumer": "FAIL:ValueError"}
        ),
    )

    with pytest.raises(RuntimeError, match="consumer validation failed"):
        activation.main([
            "--incoming-package", str(tmp_path),
            "--store-root", str(tmp_path / "store"),
            "--validate-only",
        ])

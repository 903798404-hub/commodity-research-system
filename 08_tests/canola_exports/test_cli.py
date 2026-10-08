from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agri_research_agent.canola_exports import data, update
from test_exports import source


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("canola_exports_cli", ROOT / "04_scripts/canola_exports/update_exports.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def local_root(tmp_path):
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps({
        "schema_version": 1, "runtime_id": "canola-local-test", "classification": "isolated-dev",
        "module_id": data.MODULE_ID, "created_at": update.now(),
    }), encoding="utf-8")
    return tmp_path


def command(root, *args):
    return cli.main(["--runtime-root", str(root), *args])


def test_one_command_updates_latest_two_years_and_no_change_preserves_stable(local_root, monkeypatch):
    monkeypatch.setattr(cli, "discover_years", lambda: ["2026-2027", "2025-2026", "2024-2025"])
    fetched = []

    def fetch(url):
        fetched.append(url)
        return source("2026-2027" if url == data.source_url("2026-2027") else "2025-2026")

    monkeypatch.setattr(cli, "official_bytes", fetch)
    monkeypatch.setattr(cli, "missing_reports", lambda downloads: {})
    assert command(local_root, "update-local") == 0
    assert fetched == [data.source_url("2026-2027"), data.source_url("2025-2026")]
    stable = local_root / data.STABLE
    previous = stable.read_bytes()
    assert len(data.load_bundle(stable)["sources"]) == 2
    assert command(local_root, "update-local") == 0
    assert stable.read_bytes() == previous
    assert json.loads((local_root / data.STATUS).read_text())["status"] == "NO_CHANGE"


def test_prepare_does_not_activate_and_offline_update_uses_only_source_file(local_root, monkeypatch):
    raw = local_root / "input.csv"
    raw.write_bytes(source())

    def no_network(*args, **kwargs):
        raise AssertionError("offline import must not fetch reports or discover years")

    for name in ("official_bytes", "missing_reports", "discover_years"):
        monkeypatch.setattr(cli, name, no_network)
    args = ["--crop-year", "2026-2027", "--source-file", str(raw)]
    assert command(local_root, "prepare", *args) == 0
    assert not (local_root / data.STABLE).exists()
    assert command(local_root, "update-local", *args) == 0
    assert (local_root / data.STABLE).is_file()


def test_failed_weekly_download_keeps_published_data(local_root, monkeypatch):
    raw = local_root / "input.csv"
    raw.write_bytes(source())
    assert command(local_root, "update-local", "--crop-year", "2026-2027", "--source-file", str(raw)) == 0
    stable = local_root / data.STABLE
    previous = stable.read_bytes()
    monkeypatch.setattr(cli, "discover_years", lambda: ["2026-2027"])

    def unavailable(url):
        raise OSError("CGC unavailable")

    monkeypatch.setattr(cli, "official_bytes", unavailable)
    assert command(local_root, "update-local") == 1
    assert stable.read_bytes() == previous
    assert json.loads((local_root / data.STATUS).read_text())["status"] == "FAILED"


def test_formal_root_rejected_before_network(local_root, monkeypatch):
    marker = local_root / ".market-data-runtime.json"
    value = json.loads(marker.read_text())
    value["classification"] = "formal"
    marker.write_text(json.dumps(value), encoding="utf-8")

    def no_network(*args, **kwargs):
        raise AssertionError("formal runtime must be rejected before downloads")

    monkeypatch.setattr(cli, "discover_years", no_network)
    assert command(local_root, "update-local") == 1
    assert not (local_root / data.STABLE).exists()

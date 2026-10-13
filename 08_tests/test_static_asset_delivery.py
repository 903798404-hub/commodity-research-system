"""Lossless build assets, service lifecycle and identity-before-HTTP consumers."""
from __future__ import annotations

import gzip
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_precompressed_assets_are_lossless_deterministic_and_preserve_originals(tmp_path):
    build = load_script("asset_build", "04_scripts/runtime/precompress_streamlit_assets.py")
    script = tmp_path / "chart.js"
    source = ('const description = "图表口径保持原样";\n' * 200).encode("utf-8")
    script.write_bytes(source)
    os.utime(script, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    png = tmp_path / "chart.png"
    png.write_bytes(b"already compressed image" * 200)
    small = tmp_path / "tiny.js"
    small.write_bytes(b"export default 1;")
    result = build.precompress_assets(tmp_path)
    sidecar = tmp_path / "chart.js.gz"
    first = sidecar.read_bytes()
    assert gzip.decompress(first) == script.read_bytes() == source
    assert len(first) < len(source)
    assert sidecar.stat().st_mtime_ns == script.stat().st_mtime_ns
    assert result["files"] == 1
    build.precompress_assets(tmp_path)
    assert sidecar.read_bytes() == first
    assert png.read_bytes() == b"already compressed image" * 200
    assert not (tmp_path / "chart.png.gz").exists()
    assert not (tmp_path / "tiny.js.gz").exists()


def record_children(monkeypatch, gateway):
    children = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(gateway.subprocess, "Popen", launch)
    return children, launch


@pytest.mark.parametrize("exit_code", [0, 7])
def test_an_exited_worker_fails_the_service_and_stops_its_sibling(monkeypatch, exit_code):
    gateway = load_script("asset_gateway_failure", "04_scripts/runtime/serve_spread_dashboard.py")
    children, _ = record_children(monkeypatch, gateway)
    commands = [
        [sys.executable, "-c", f"import time; time.sleep(.3); raise SystemExit({exit_code})"],
        [sys.executable, "-c", "import time; time.sleep(60)"],
    ]
    assert gateway.supervise(commands, threading.Event()) == (exit_code or 1)
    assert len(children) == 2 and all(child.poll() is not None for child in children)


def test_a_stop_request_closes_all_owned_workers(monkeypatch):
    gateway = load_script("asset_gateway_stop", "04_scripts/runtime/serve_spread_dashboard.py")
    children, _ = record_children(monkeypatch, gateway)
    stop = threading.Event()
    timer = threading.Timer(.3, stop.set)
    timer.start()
    try:
        assert gateway.supervise([[sys.executable, "-c", "import time; time.sleep(60)"]] * 2, stop) == 0
    finally:
        timer.cancel()
        timer.join()
    assert len(children) == 2 and all(child.poll() is not None for child in children)


def test_a_failed_second_launch_stops_the_first_worker(monkeypatch):
    gateway = load_script("asset_gateway_start_failure", "04_scripts/runtime/serve_spread_dashboard.py")
    children, launch = record_children(monkeypatch, gateway)

    def fail_second(*args, **kwargs):
        if children:
            raise FileNotFoundError("missing gateway executable")
        return launch(*args, **kwargs)

    monkeypatch.setattr(gateway.subprocess, "Popen", fail_second)
    with pytest.raises(FileNotFoundError):
        gateway.supervise([[sys.executable, "-c", "import time; time.sleep(60)"]] * 2, threading.Event())
    assert len(children) == 1 and children[0].poll() is not None


def test_http_preflight_cannot_run_before_successful_execution_identity(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "04_scripts"))
    from runtime import serve_spread_dashboard as gateway
    preflight = load_script("asset_identity_preflight", "04_scripts/runtime/spread_runtime_preflight.py")
    monkeypatch.setattr(gateway, "verify_static_http_delivery", lambda: pytest.fail("HTTP before identity"))
    def rejected(_args):
        raise ValueError("invalid execution identity")
    monkeypatch.setattr(preflight, "readonly_preflight", rejected)
    assert preflight.main(["--identity-kind", "oci_container", "--verify-http-delivery"]) == 1
    assert '"status": "FAILED"' in capsys.readouterr().out


def test_http_delivery_failure_fails_the_required_initialization(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "04_scripts"))
    from runtime import serve_spread_dashboard as gateway
    preflight = load_script("asset_http_preflight", "04_scripts/runtime/spread_runtime_preflight.py")
    monkeypatch.setattr(preflight, "readonly_preflight", lambda _args: {"status": "PASS"})
    def uncompressed():
        raise RuntimeError("gzip was not served")
    monkeypatch.setattr(gateway, "verify_static_http_delivery", uncompressed)
    assert preflight.main(["--identity-kind", "oci_container", "--verify-http-delivery"]) == 1
    assert '"status": "FAILED"' in capsys.readouterr().out

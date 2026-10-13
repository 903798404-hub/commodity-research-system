"""Supervise the unchanged Streamlit app and its static-only gzip gateway."""
from __future__ import annotations

import gzip
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


SOURCE_ROOT = Path(__file__).resolve().parents[2]
APP_SCRIPT = "05_apps/streamlit_app.py"
NGINX_CONFIG = "09_deploy/spread_runtime/nginx.conf"
LOG_ROOT = Path("/runtime/10_logs")
SHUTDOWN_GRACE_SECONDS = 8


def stop_children(children: list[subprocess.Popen]) -> None:
    """Stop only process groups created by this launcher, including nginx workers."""
    for child in children:
        if child.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except ProcessLookupError:
                pass
            child.wait()


def supervise(commands: list[list[str]], stop: threading.Event) -> int:
    children: list[subprocess.Popen] = []
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=SOURCE_ROOT, start_new_session=os.name == "posix"))
        while not stop.wait(0.1):
            for child in children:
                code = child.poll()
                if code is not None:
                    print(f"Dashboard service child exited: {code}", file=sys.stderr, flush=True)
                    return code if code > 0 else 1
        return 0
    finally:
        stop_children(children)


def verify_static_http_delivery() -> dict:
    """Read-only real HTTP checks, executed inside the identity-verified container."""
    import streamlit

    base = "http://127.0.0.1:8501"
    # Do not use proxy variables for the container's own loopback health check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError("Dashboard HTTP health did not become ready")
        try:
            with opener.open(base + "/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)

    assets = Path(streamlit.__file__).resolve().parent / "static" / "static" / "js"
    paths = [next(assets.glob("index.*.js")), next(assets.glob("PlotlyChart.*.js"))]
    results = []
    for path in paths:
        url = base + "/static/js/" + path.name
        original = path.read_bytes()
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with opener.open(req, timeout=10) as response:
            zipped = response.read()
            if response.headers.get("Content-Encoding") != "gzip":
                raise RuntimeError("Precompressed frontend asset was not served")
            if "accept-encoding" not in response.headers.get("Vary", "").lower():
                raise RuntimeError("Compressed asset lacks its cache representation header")
            if "immutable" not in response.headers.get("Cache-Control", ""):
                raise RuntimeError("Immutable frontend cache policy changed")
            if gzip.decompress(zipped) != original or len(zipped) >= len(original):
                raise RuntimeError("Frontend compressed bytes do not reproduce the original")
            if int(response.headers["Content-Length"]) != len(zipped):
                raise RuntimeError("Compressed Content-Length is incorrect")
        # A client declining gzip must receive exactly the original bytes.
        request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip;q=0, identity"})
        with opener.open(request, timeout=10) as response:
            if response.headers.get("Content-Encoding") or response.read() != original:
                raise RuntimeError("Non-gzip asset negotiation changed the frontend")
        results.append(dict(file=path.name, original_bytes=len(original), gzip_bytes=len(zipped)))
    return dict(status="PASS", assets=results)


def main() -> int:
    if os.name != "posix":
        raise RuntimeError("The packaged dashboard service requires Linux")
    for child_dir in ("nginx-body", "nginx-proxy"):
        (LOG_ROOT / child_dir).mkdir(parents=True, exist_ok=True)
    nginx = ["nginx", "-c", str(SOURCE_ROOT / NGINX_CONFIG)]
    subprocess.run([*nginx, "-t"], check=True)
    commands = [
        [sys.executable, "-B", "-m", "streamlit", "run", APP_SCRIPT,
         "--server.address=127.0.0.1", "--server.port=8502"],
        [*nginx, "-g", "daemon off;"],
    ]
    stop = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_args: stop.set())
    try:
        return supervise(commands, stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())

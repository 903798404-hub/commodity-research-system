from __future__ import annotations

import importlib.util
from pathlib import Path
import re

from packaging.markers import Marker


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "04_scripts" / "environment" / "merge_production_platform_lock.py"
SPEC = importlib.util.spec_from_file_location("merge_production_platform_lock", SCRIPT)
assert SPEC and SPEC.loader
MERGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MERGER)


def _entry(lock: str, name: str) -> str:
    pattern = re.compile(
        rf"(?ms)^{re.escape(name)}==.*?(?=^[A-Za-z0-9_.-]+==|\Z)"
    )
    match = pattern.search(lock)
    assert match, f"missing lock entry: {name}"
    return match.group(0)


def test_runtime_input_declares_exact_akshare_platform_dependencies() -> None:
    runtime_input = (ROOT / "requirements.in").read_text(encoding="utf-8")
    assert "akshare==1.18.64" in runtime_input
    assert "pyarrow==24.0.0" in runtime_input
    assert "psycopg[binary]==3.3.4" in runtime_input
    assert 'mini-racer==0.14.1 ; platform_system != "Linux"' in runtime_input
    assert 'py-mini-racer==0.6.0 ; platform_system == "Linux"' in runtime_input
    assert 'akracer==0.0.14 ; platform_system == "Linux"' in runtime_input


def test_tankan_postgres_runtime_dependencies_are_pinned_and_hashed() -> None:
    lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for name in ("psycopg", "psycopg-binary"):
        block = _entry(lock, name)
        assert block.startswith(f"{name}==3.3.4")
        assert "--hash=sha256:" in block


def test_shared_lock_has_pinned_hashed_mutually_exclusive_platform_entries() -> None:
    lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    expected = {
        "mini-racer": ("0.14.1", 'platform_system != "Linux"'),
        "py-mini-racer": ("0.6.0", 'platform_system == "Linux"'),
        "akracer": ("0.0.14", 'platform_system == "Linux"'),
    }
    for name, (version, marker) in expected.items():
        block = _entry(lock, name)
        assert block.startswith(f"{name}=={version} ; {marker}")
        assert "--hash=sha256:" in block
    assert Marker('platform_system == "Linux"').evaluate(
        {"platform_system": "Linux"}
    )
    assert not Marker('platform_system != "Linux"').evaluate(
        {"platform_system": "Linux"}
    )
    assert Marker('platform_system != "Linux"').evaluate(
        {"platform_system": "Windows"}
    )
    assert not Marker('platform_system == "Linux"').evaluate(
        {"platform_system": "Windows"}
    )


def test_runtime_lock_has_no_unpinned_vcs_or_local_requirements() -> None:
    lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "git+" not in lock
    assert "file://" not in lock
    for line in lock.splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "--")):
            assert re.match(r"^[A-Za-z0-9_.-]+==[^ ;\\]+(?:\s*;.*)?(?: \\)?$", line)


def test_controlled_merge_changes_only_platform_dependency_blocks() -> None:
    baseline = """# header
akshare==1.18.64 \\
    --hash=sha256:aaa
colorama==0.4.6 \\
    --hash=sha256:bbb
mini-racer==0.14.1 \\
    --hash=sha256:ccc
pyarrow==25.0.0 \\
    --hash=sha256:ddd
"""
    linux = """akracer==0.0.14 ; platform_system == "Linux" \\
    --hash=sha256:eee
akshare==1.18.64 \\
    --hash=sha256:aaa
py-mini-racer==0.6.0 ; platform_system == "Linux" \\
    --hash=sha256:fff
pyarrow==25.0.0 \\
    --hash=sha256:ddd
"""
    merged = MERGER.merge_lock_text(baseline, linux)
    assert "colorama==0.4.6" in merged
    assert 'mini-racer==0.14.1 ; platform_system != "Linux"' in merged
    assert 'py-mini-racer==0.6.0 ; platform_system == "Linux"' in merged
    assert 'akracer==0.0.14 ; platform_system == "Linux"' in merged

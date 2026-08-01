"""Merge audited Linux-only AKShare dependencies into the shared runtime lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile


ENTRY_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^ ;\\]+)(?P<suffix>.*)$"
)
TARGETS = {
    "akracer": ("0.0.14", 'platform_system == "Linux"'),
    "mini-racer": ("0.14.1", 'platform_system != "Linux"'),
    "py-mini-racer": ("0.6.0", 'platform_system == "Linux"'),
}


class PlatformLockError(ValueError):
    """Raised when either lock cannot satisfy the controlled merge contract."""


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _entries(lines: list[str]) -> dict[str, tuple[int, int, str, str]]:
    starts: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = ENTRY_RE.match(line)
        if match:
            starts.append(
                (index, _canonical(match.group("name")), match.group("version"))
            )
    result: dict[str, tuple[int, int, str, str]] = {}
    for position, (start, name, version) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        if name in result:
            raise PlatformLockError(f"duplicate requirement entry: {name}")
        result[name] = (start, end, version, lines[start])
    return result


def _require_target(
    lines: list[str], name: str, version: str, marker: str
) -> tuple[int, int]:
    entry = _entries(lines).get(name)
    if entry is None:
        raise PlatformLockError(f"Linux lock is missing {name}")
    start, end, actual_version, first_line = entry
    if actual_version != version or f"; {marker}" not in first_line:
        raise PlatformLockError(f"Linux lock has an invalid {name} requirement")
    block = lines[start:end]
    if not any("--hash=sha256:" in line for line in block):
        raise PlatformLockError(f"Linux lock has no SHA-256 hash for {name}")
    return start, end


def merge_lock_text(baseline_text: str, linux_text: str) -> str:
    """Return a shared lock with only the audited platform dependency changes."""

    baseline = baseline_text.splitlines(keepends=True)
    linux = linux_text.splitlines(keepends=True)
    baseline_entries = _entries(baseline)
    linux_entries = _entries(linux)

    for name, (version, marker) in TARGETS.items():
        if name == "mini-racer":
            continue
        _require_target(linux, name, version, marker)
    if "mini-racer" in linux_entries:
        raise PlatformLockError("Linux lock must not install mini-racer")

    mini = baseline_entries.get("mini-racer")
    if mini is None or mini[2] != TARGETS["mini-racer"][0]:
        raise PlatformLockError("baseline lock must contain mini-racer==0.14.1")
    for name in ("akshare", "pyarrow"):
        if name not in baseline_entries:
            raise PlatformLockError(f"baseline lock is missing insertion anchor {name}")

    before_versions = {
        name: entry[2]
        for name, entry in baseline_entries.items()
        if name not in TARGETS
    }
    for name in ("akracer", "py-mini-racer"):
        entry = baseline_entries.get(name)
        if entry:
            del baseline[entry[0] : entry[1]]
            baseline_entries = _entries(baseline)

    mini = baseline_entries["mini-racer"]
    continuation = " \\\n" if baseline[mini[0]].endswith("\\\n") else "\n"
    baseline[mini[0]] = (
        'mini-racer==0.14.1 ; platform_system != "Linux"' + continuation
    )

    linux_entries = _entries(linux)
    akracer = linux[slice(*linux_entries["akracer"][:2])]
    py_mini = linux[slice(*linux_entries["py-mini-racer"][:2])]
    baseline_entries = _entries(baseline)
    baseline[baseline_entries["akshare"][0] : baseline_entries["akshare"][0]] = akracer
    baseline_entries = _entries(baseline)
    baseline[baseline_entries["pyarrow"][0] : baseline_entries["pyarrow"][0]] = py_mini

    merged = "".join(baseline)
    final_entries = _entries(merged.splitlines(keepends=True))
    after_versions = {
        name: entry[2]
        for name, entry in final_entries.items()
        if name not in TARGETS
    }
    if after_versions != before_versions:
        raise PlatformLockError("merge changed an unrelated package or version")
    for name, (version, marker) in TARGETS.items():
        entry = final_entries.get(name)
        if entry is None or entry[2] != version or f"; {marker}" not in entry[3]:
            raise PlatformLockError(f"merged lock has an invalid {name} requirement")
    return merged


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge audited Linux AKShare dependencies into a shared lock."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--linux-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline = args.baseline.read_text(encoding="utf-8")
    linux = args.linux_lock.read_text(encoding="utf-8")
    merged = merge_lock_text(baseline, linux)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{args.output.name}.", dir=args.output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(merged)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

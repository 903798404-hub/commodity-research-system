#!/usr/bin/env python
"""Interactively initialize the machine-local Lutou credential file."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from uuid import uuid4


DEFAULT_SECRET_FILE = Path.home() / ".market-data-secrets" / "lutou.env"


def _prompt_value(label: str, *, hidden: bool = False) -> str:
    value = getpass.getpass(f"{label}: ") if hidden else input(f"{label}: ")
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise SystemExit(f"{label} is invalid")
    if value != value.strip('"').strip("'"):
        raise SystemExit(f"{label} is invalid")
    return value


def initialize(secret_file: Path) -> None:
    if secret_file.exists():
        raise SystemExit("Lutou secret already exists; refusing to overwrite")
    host = _prompt_value("LUTOU_HOST")
    port_text = _prompt_value("LUTOU_PORT")
    user = _prompt_value("LUTOU_USER")
    password = _prompt_value("LUTOU_PASSWORD", hidden=True)
    try:
        port = int(port_text)
    except ValueError:
        raise SystemExit("LUTOU_PORT is invalid") from None
    if not 1 <= port <= 65535:
        raise SystemExit("LUTOU_PORT is invalid")
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        f"LUTOU_HOST={host}\n"
        f"LUTOU_PORT={port}\n"
        f"LUTOU_USER={user}\n"
        f"LUTOU_PASSWORD={password}\n"
    )
    temporary = secret_file.parent / f".{secret_file.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, secret_file)
        except FileExistsError:
            raise SystemExit(
                "Lutou secret already exists; refusing to overwrite"
            ) from None
    finally:
        temporary.unlink(missing_ok=True)
        password = ""
        payload = ""
    print("LUTOU SECRET INITIALIZED")


def main() -> int:
    initialize(DEFAULT_SECRET_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

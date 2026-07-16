from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from agri_research_agent.pipelines.update_basis_data import (
    apply_basis_update,
    load_basis_update_config,
)


def update_basis_from_upload(
    uploaded_bytes: bytes,
    uploaded_name: str,
    *,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """Place a web upload at the configured inbox and use the same safe updater."""
    if Path(uploaded_name).suffix.lower() != ".xlsx":
        raise ValueError("只允许上传 .xlsx 文件")
    if not uploaded_bytes:
        raise ValueError("上传文件为空")

    settings = load_basis_update_config(config_path)
    incoming = settings["incoming_excel"]
    incoming.parent.mkdir(parents=True, exist_ok=True)
    temporary = incoming.parent / f".{incoming.stem}.{uuid.uuid4().hex}.tmp.xlsx"
    try:
        temporary.write_bytes(uploaded_bytes)
        os.replace(temporary, incoming)
        return apply_basis_update(incoming, config_path=config_path)
    finally:
        temporary.unlink(missing_ok=True)
        incoming.unlink(missing_ok=True)

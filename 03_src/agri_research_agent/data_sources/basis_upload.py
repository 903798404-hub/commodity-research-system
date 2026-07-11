from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from openpyxl import load_workbook

from agri_research_agent.core.paths import (
    BASIS_DATABASE_FILE,
    BASIS_EXCEL_FILE,
    CONFIG_DIR,
)
from agri_research_agent.data_sources.basis_database import (
    build_basis_database,
)


def update_basis_from_upload(
    uploaded_bytes: bytes,
    uploaded_name: str,
    *,
    source_path: Path = BASIS_EXCEL_FILE,
    backup_dir: Path | None = None,
    config_path: Path | None = None,
    output_path: Path = BASIS_DATABASE_FILE,
    now: datetime | None = None,
) -> dict[str, Any]:
    if Path(uploaded_name).suffix.lower() != ".xlsx":
        raise ValueError("只允许上传 .xlsx 文件")
    if not uploaded_bytes:
        raise ValueError("上传文件为空")

    resolved_backup_dir = backup_dir or source_path.parent / "backup"
    resolved_config = config_path or CONFIG_DIR / "basis_excel.yaml"
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    backup_path = (
        resolved_backup_dir / f"国内现货基差_{timestamp}.xlsx"
    )
    lock_path = source_path.parent / ".basis_upload.lock"

    source_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_backup_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_file: Path | None = None
    with FileLock(str(lock_path), timeout=60):
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".xlsx",
                prefix="basis_upload_",
                dir=source_path.parent,
                delete=False,
            ) as file:
                file.write(uploaded_bytes)
                temporary_file = Path(file.name)

            try:
                workbook = load_workbook(
                    temporary_file,
                    read_only=True,
                    data_only=False,
                )
                workbook.close()
            except Exception as exc:
                raise ValueError(
                    f"上传的 Excel 无法读取，请确认文件未损坏：{exc}"
                ) from exc

            source_existed = source_path.exists()
            if source_existed:
                shutil.copy2(source_path, backup_path)

            os.replace(temporary_file, source_path)
            temporary_file = None
            try:
                result = build_basis_database(
                    config_path=resolved_config,
                    output_path=output_path,
                )
            except Exception:
                if source_existed and backup_path.exists():
                    shutil.copy2(backup_path, source_path)
                elif source_path.exists():
                    source_path.unlink()
                raise
        finally:
            if temporary_file is not None and temporary_file.exists():
                temporary_file.unlink()

    modified_time = datetime.fromtimestamp(output_path.stat().st_mtime)
    return {
        **result,
        "backup_file": backup_path if backup_path.exists() else None,
        "modified_time": modified_time,
    }

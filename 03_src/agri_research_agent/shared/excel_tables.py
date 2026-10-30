"""Bounded-memory writes for generated flat tables, never manual workbooks."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd


def _cell(value):
    # Match the existing pandas-to-Excel scalar convention, including real zero,
    # deliberate blanks, infinities and Excel dates. No business calculation.
    if isinstance(value, (list, tuple, dict)):
        return str(value)
    if value is None or pd.isna(value):
        # pandas uses na_rep="": retain explicit trailing blank cells and the
        # original rectangular worksheet shape as well as the blank value.
        return ""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, float) and math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if getattr(value, "tzinfo", None) is not None:
        raise ValueError("Excel tables cannot contain timezone-aware dates")
    return value


def write_tables(path: Path, tables) -> None:
    """Write flat generated tables without retaining millions of Excel cells."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Border, Font, Side

    book = Workbook(write_only=True)
    try:
        for name, frame in tables:
            if isinstance(frame.columns, pd.MultiIndex):
                raise ValueError("Generated table columns must be flat")
            sheet = book.create_sheet(name)
            header = []
            for label in frame.columns:
                cell = WriteOnlyCell(sheet, value=_cell(label))
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center", vertical="top")
                side = Side(style="thin")
                cell.border = Border(left=side, right=side, top=side, bottom=side)
                header.append(cell)
            if header:
                sheet.append(header)
            for row in frame.itertuples(index=False, name=None):
                sheet.append([_cell(value) for value in row])
        book.save(path)
    finally:
        book.close()


def rewritten_tables(path: Path, replacements: dict, transforms=None):
    """Read one retained sheet at a time; replaced sheets are never loaded."""
    with pd.ExcelFile(path) as source:
        if not set(replacements).issubset(source.sheet_names):
            raise ValueError("Generated workbook is missing a replaced sheet")
        for name in source.sheet_names:
            frame = replacements[name] if name in replacements else source.parse(name)
            if transforms and name in transforms:
                frame = transforms[name](frame)
            yield name, frame

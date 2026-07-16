from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from agri_research_agent.data_sources.basis_database import build_basis_database
from agri_research_agent.pipelines.update_basis_data import apply_basis_update


FORMAL_COMMODITIES = ["一豆", "三菜", "24度", "豆粕", "菜粕"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "05_apps"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))


def _row(
    date: str,
    commodity: str,
    *,
    cash_price: float = 3100,
    futures_price: float = 3000,
) -> dict[str, object]:
    return {
        "日期": date,
        "区域": "华东",
        "提货月": "现货",
        "对应合约月": "09月",
        "盘面价": futures_price,
        "一口价（元/吨）": cash_price,
        "基差": cash_price - futures_price,
        "commodity": commodity,
    }


def _write_workbook(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for commodity in FORMAL_COMMODITIES:
            sheet_rows = [
                {key: value for key, value in row.items() if key != "commodity"}
                for row in rows
                if row["commodity"] == commodity
            ]
            pd.DataFrame(sheet_rows).to_excel(
                writer,
                sheet_name=f"{commodity}基差",
                index=False,
            )


def _write_config(root: Path) -> Path:
    current_excel = root / "manual" / "国内现货基差.xlsx"
    config = {
        "source_file": {"path": str(current_excel)},
        "update": {
            "incoming_path": str(root / "incoming" / "国内现货基差.xlsx"),
            "parquet_path": str(root / "database" / "basis_quotes.parquet"),
            "status_path": str(root / "update_status.json"),
            "primary_key": [
                "date",
                "commodity",
                "region",
                "quote_type",
                "delivery_month",
                "futures_contract",
            ],
            "formal_display_commodities": {
                "一豆": "豆油",
                "三菜": "菜油",
                "24度": "棕榈油",
                "豆粕": "豆粕",
                "菜粕": "菜粕",
            },
            "excluded_display_commodities": [],
        },
        "basis_sheets": {
            f"{commodity}基差": {"commodity": commodity, "type": "basis"}
            for commodity in FORMAL_COMMODITIES
        },
        "cash_price_sheets": {},
    }
    config_path = root / "basis_excel.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def update_case(tmp_path: Path) -> dict[str, object]:
    config_path = _write_config(tmp_path)
    current_excel = tmp_path / "manual" / "国内现货基差.xlsx"
    current_parquet = tmp_path / "database" / "basis_quotes.parquet"
    incoming = tmp_path / "incoming" / "国内现货基差.xlsx"
    status = tmp_path / "update_status.json"
    baseline_rows = [
        _row("2026-06-01", commodity) for commodity in FORMAL_COMMODITIES
    ] + [_row("2026-06-02", "一豆", cash_price=3110)]
    _write_workbook(current_excel, baseline_rows)
    build_basis_database(config_path, current_parquet, current_excel)
    status.write_text('{"status": "success"}\n', encoding="utf-8")
    return {
        "root": tmp_path,
        "config": config_path,
        "excel": current_excel,
        "parquet": current_parquet,
        "incoming": incoming,
        "status": status,
        "rows": baseline_rows,
    }


def _formal_bytes(case: dict[str, object]) -> tuple[bytes, bytes, bytes]:
    return (
        Path(case["excel"]).read_bytes(),
        Path(case["parquet"]).read_bytes(),
        Path(case["status"]).read_bytes(),
    )


def test_normal_append_and_same_excel_are_idempotent(
    update_case: dict[str, object],
) -> None:
    incoming = Path(update_case["incoming"])
    rows = [*update_case["rows"], _row("2026-06-03", "一豆", cash_price=3120)]
    _write_workbook(incoming, rows)

    first = apply_basis_update(
        config_path=Path(update_case["config"]),
        project_root=Path(update_case["root"]),
    )
    assert first["added_rows"] == 1
    assert first["candidate_rows"] == 7
    assert not incoming.exists()

    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(Path(update_case["excel"]).read_bytes())
    second = apply_basis_update(
        config_path=Path(update_case["config"]),
        project_root=Path(update_case["root"]),
    )
    assert second["added_rows"] == 0
    assert second["candidate_rows"] == 7
    assert len(pd.read_parquet(Path(update_case["parquet"]))) == 7


def test_history_removal_is_rejected(update_case: dict[str, object]) -> None:
    incoming = Path(update_case["incoming"])
    rows = [
        row
        for row in update_case["rows"]
        if not (row["commodity"] == "一豆" and row["日期"] == "2026-06-02")
    ]
    _write_workbook(incoming, rows)
    before = _formal_bytes(update_case)

    with pytest.raises(ValueError, match="删除了 1 条历史记录"):
        apply_basis_update(
            config_path=Path(update_case["config"]),
            project_root=Path(update_case["root"]),
        )

    assert _formal_bytes(update_case) == before
    assert not incoming.exists()


def test_historical_value_change_is_rejected(
    update_case: dict[str, object],
) -> None:
    incoming = Path(update_case["incoming"])
    rows = [dict(row) for row in update_case["rows"]]
    rows[0]["一口价（元/吨）"] = 3200
    rows[0]["基差"] = 200
    _write_workbook(incoming, rows)
    before = _formal_bytes(update_case)

    with pytest.raises(ValueError, match="改写了 1 条历史记录"):
        apply_basis_update(
            config_path=Path(update_case["config"]),
            project_root=Path(update_case["root"]),
        )

    assert _formal_bytes(update_case) == before


def test_corrupt_excel_is_rejected(update_case: dict[str, object]) -> None:
    incoming = Path(update_case["incoming"])
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(b"not an xlsx file")
    before = _formal_bytes(update_case)

    with pytest.raises(ValueError, match="Excel 解析或 Parquet 生成失败"):
        apply_basis_update(
            config_path=Path(update_case["config"]),
            project_root=Path(update_case["root"]),
        )

    assert _formal_bytes(update_case) == before
    assert not incoming.exists()


def test_parquet_failure_keeps_formal_files(
    update_case: dict[str, object],
) -> None:
    incoming = Path(update_case["incoming"])
    _write_workbook(incoming, list(update_case["rows"]))
    before = _formal_bytes(update_case)

    def fail_build(**_: object) -> dict[str, object]:
        raise RuntimeError("simulated parquet failure")

    with pytest.raises(ValueError, match="simulated parquet failure"):
        apply_basis_update(
            config_path=Path(update_case["config"]),
            project_root=Path(update_case["root"]),
            build_function=fail_build,
        )

    assert _formal_bytes(update_case) == before
    assert not incoming.exists()


def test_success_cleans_temporary_files(update_case: dict[str, object]) -> None:
    incoming = Path(update_case["incoming"])
    _write_workbook(incoming, list(update_case["rows"]))
    apply_basis_update(
        config_path=Path(update_case["config"]),
        project_root=Path(update_case["root"]),
    )

    leftovers = [
        path
        for path in Path(update_case["root"]).rglob("*")
        if path.is_file() and (".tmp" in path.name or ".rollback" in path.name)
    ]
    assert leftovers == []
    assert not incoming.exists()
    status = json.loads(Path(update_case["status"]).read_text(encoding="utf-8"))
    assert status["basis_update"]["status"] == "success"


def test_new_parquet_supports_basis_and_cash_page_data(
    update_case: dict[str, object],
) -> None:
    from basis_page import filter_display_data, prepare_cash_price_data

    incoming = Path(update_case["incoming"])
    _write_workbook(
        incoming,
        [*update_case["rows"], _row("2026-06-03", "一豆", cash_price=3120)],
    )
    apply_basis_update(
        config_path=Path(update_case["config"]),
        project_root=Path(update_case["root"]),
    )
    data = pd.read_parquet(Path(update_case["parquet"]))
    visible = filter_display_data(data)
    cash = prepare_cash_price_data(visible)

    assert set(visible["commodity"]) == {"豆油", "菜油", "棕榈油", "豆粕", "菜粕"}
    assert visible["basis"].notna().all()
    assert cash["cash_price"].notna().all()
    assert visible["date"].max() == pd.Timestamp("2026-06-03")

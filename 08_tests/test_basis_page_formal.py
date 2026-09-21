from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"


def _formal_public_current_rows() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for offset, commodity in enumerate(("一豆", "24度", "三菜", "豆粕", "菜粕")):
        for day, basis in ((18, 100 + offset), (19, 110 + offset)):
            rows.append({
                "date": pd.Timestamp(2026, 8, day),
                "commodity": commodity,
                "region": "华东",
                "quote_type": "基差报价",
                "delivery_month": "现货",
                "futures_contract": "2609",
                "cash_price": 8000 + offset,
                "futures_price": 7900 + offset,
                "basis": basis,
                "source_sheet": f"basis_price:{commodity}",
            })
    return pd.DataFrame(rows)


def test_basis_page_uses_formal_database_and_renders_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apps_dir = str(PROJECT_ROOT / "05_apps")
    if apps_dir not in sys.path:
        sys.path.insert(0, apps_dir)
    basis_page = __import__("basis_page")
    rows = _formal_public_current_rows()
    monkeypatch.setattr(
        basis_page,
        "load_basis_page_data",
        lambda _root: (
            rows.copy(),
            {"release_id": "required-test-current", "manifest_sha256": "a" * 64},
        ),
    )

    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=40).run()
    app.session_state["selected_workspace_page"] = "基差/一口价"
    app.run(timeout=40)

    assert not app.exception
    assert not any("当前读取" in item.value for item in (*app.success, *app.info, *app.caption))
    assert {"基差", "一口价", "批发价差"}.issubset({item.label for item in app.tabs})
    assert not {"Excel 导入", "表格录入", "批量粘贴"}.intersection({item.label for item in app.tabs})
    expander_labels = [item.label for item in app.expander]
    assert "Excel 只读预览（不写入正式数据）" not in expander_labels
    assert "查看可追溯事实" not in expander_labels
    assert expander_labels.count("查看明细") >= 3
    commodity_selectboxes = [
        item for item in app.selectbox if item.label == "品种"
    ]
    assert commodity_selectboxes
    assert all(
        "葵油" not in item.options and "一葵" not in item.options
        for item in commodity_selectboxes
    )
    quote_type_selectboxes = [
        item for item in app.selectbox if item.label == "报价类型"
    ]
    assert quote_type_selectboxes
    basis_quote_type = next(
        item for item in quote_type_selectboxes if item.key == "basis_matrix_quote_type"
    )
    assert "全部" in basis_quote_type.options
    assert basis_quote_type.value == "基差报价"
    delivery_selectboxes = [
        item for item in app.selectbox if item.label == "提货月"
    ]
    assert delivery_selectboxes
    assert all(item.options == ["现货"] for item in delivery_selectboxes)
    assert all(item.value == "现货" for item in delivery_selectboxes)
    assert any(
        "数据源：Formal Public Basis Current" in item.value
        and "数据更新至" in item.value
        for item in app.caption
    )
    expected_latest = rows["date"].max().date().isoformat()
    assert any(expected_latest in item.value for item in app.caption)
    assert not any("本地回退文件可用" in item.value for item in (*app.success, *app.info, *app.caption))
    assert not any("本地回退数据" in item.value for item in (*app.success, *app.info, *app.caption))
    assert not any(item.label in {"数据状态", "总行数", "最新日期"} for item in app.metric)
    assert any(item.value == "最新基差" for item in app.subheader)
    latest_table = app.dataframe[0].value
    assert {"品种", "地区", "报价类型", "交货月", "期货合约", "现货价", "期货价", "基差"}.issubset(latest_table.columns)
    assert {"豆油", "豆粕", "棕榈油", "菜油", "菜粕"}.issuperset(set(latest_table["品种"]))
    assert latest_table["地区"].notna().all()
    assert latest_table["基差"].str.contains(r"（(?:[+-]?\d+(?:\.\d+)?|换月|无前值)）", regex=True).all()
    visible_text = " ".join(str(item.value) for item in (*app.caption, *app.info, *app.success, *app.warning))
    for hidden in ("手工录入", "Excel 导入", "表格录入", "批量粘贴", "当前读取", "本地回退文件可用", "查看可追溯事实"):
        assert hidden not in visible_text
    assert not any(
        "等待接入" in item.value
        for item in (*app.info, *app.warning, *app.error)
    )


def test_legacy_entry_preview_cannot_write_formal_parquet() -> None:
    source = (PROJECT_ROOT / "05_apps" / "basis_page.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def _render_shared_preview()")
    end = source.index("def _render_legacy_excel_upload()", start)
    preview_source = source[start:end]

    assert "write_confirmed(" not in preview_source
    assert preview_source.count("disabled=True") >= 2


def test_basis_page_missing_public_current_fails_closed_without_fallback(
    tmp_path: Path,
) -> None:
    script = f"""
from pathlib import Path
from basis_page import render_basis_page
render_basis_page(Path({str(tmp_path / 'missing-current')!r}))
"""
    app = AppTest.from_string(script, default_timeout=15).run()
    assert not app.exception
    assert "Formal Public Basis Current 暂不可用。" in [
        item.value for item in app.warning
    ]
    assert not app.tabs
    visible = " ".join(
        str(item.value) for item in (*app.warning, *app.info, *app.caption)
    )
    assert "回退" not in visible

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_ENTRY = PROJECT_ROOT / "05_apps" / "streamlit_app.py"


def test_basis_page_uses_formal_database_and_renders_modules() -> None:
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=20).run()
    app.session_state["selected_workspace_page"] = "基差/一口价"
    app.run(timeout=20)

    assert not app.exception
    assert any("当前读取：正式数据" in item.value for item in app.success)
    assert {"基差", "一口价", "批发价差", "Excel 导入", "表格录入", "批量粘贴"}.issubset({item.label for item in app.tabs})
    expander_labels = [item.label for item in app.expander]
    assert expander_labels.count("Excel 只读预览（不写入正式数据）") == 1
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
        "2026-06-01前为历史基差库" in item.value
        and "2026-06-01起为basis_price现货基差" in item.value
        for item in app.caption
    )
    assert any(
        item.label == "最新日期" and item.value == "2026-08-04"
        for item in app.metric
    )
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

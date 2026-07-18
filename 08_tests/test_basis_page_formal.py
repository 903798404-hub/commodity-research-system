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
    assert expander_labels.count("上传更新国内基差 Excel") == 1
    assert expander_labels.count("查看明细") >= 3
    commodity_selectboxes = [
        item for item in app.selectbox if item.label == "品种"
    ]
    assert commodity_selectboxes
    assert all(
        "葵油" not in item.options and "一葵" not in item.options
        for item in commodity_selectboxes
    )
    assert any(
        item.label == "报价类型" and item.value == "全部"
        for item in app.selectbox
    )
    assert not any(
        "等待接入" in item.value
        for item in (*app.info, *app.warning, *app.error)
    )

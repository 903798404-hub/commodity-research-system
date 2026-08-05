from __future__ import annotations

import matplotlib as mpl
import pytest
from matplotlib import font_manager

from agri_research_agent.utils.matplotlib_config import (
    CHINESE_FONT_CANDIDATES,
    configure_matplotlib_chinese_fonts,
)


def test_installed_chinese_matplotlib_font_is_configured() -> None:
    selected = configure_matplotlib_chinese_fonts()
    installed = {font.name for font in font_manager.fontManager.ttflist}

    assert len(selected) == 1
    assert selected[0] in CHINESE_FONT_CANDIDATES
    assert selected[0] in installed
    assert list(mpl.rcParams["font.sans-serif"]) == selected
    assert mpl.rcParams["axes.unicode_minus"] is False


def test_missing_chinese_font_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(font_manager.fontManager, "ttflist", [])

    with pytest.raises(RuntimeError, match="未发现可用中文字体"):
        configure_matplotlib_chinese_fonts()

from __future__ import annotations

import matplotlib as mpl

from agri_research_agent.utils.matplotlib_config import (
    CHINESE_FONT_FALLBACKS,
    configure_matplotlib_chinese_fonts,
)


def test_chinese_matplotlib_font_fallbacks_are_configured() -> None:
    assert configure_matplotlib_chinese_fonts() == CHINESE_FONT_FALLBACKS
    assert list(mpl.rcParams["font.sans-serif"]) == CHINESE_FONT_FALLBACKS
    assert mpl.rcParams["axes.unicode_minus"] is False

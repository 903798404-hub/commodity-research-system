"""Shared Matplotlib configuration for Chinese-language charts."""

from __future__ import annotations

import matplotlib as mpl
from matplotlib import font_manager


CHINESE_FONT_CANDIDATES = [
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "WenQuanYi Zen Hei",
    "Arial Unicode MS",
]


def configure_matplotlib_chinese_fonts() -> list[str]:
    """Select an installed Chinese font and configure Matplotlib."""
    installed = {font.name for font in font_manager.fontManager.ttflist}
    selected = next(
        (name for name in CHINESE_FONT_CANDIDATES if name in installed),
        None,
    )
    if selected is None:
        raise RuntimeError(
            "未发现可用中文字体；候选为："
            + "、".join(CHINESE_FONT_CANDIDATES)
        )
    mpl.rcParams["font.sans-serif"] = [selected]
    mpl.rcParams["axes.unicode_minus"] = False
    return [selected]

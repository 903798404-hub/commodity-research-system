"""Shared Matplotlib configuration for Chinese-language charts."""

from __future__ import annotations

import matplotlib as mpl


CHINESE_FONT_FALLBACKS = [
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Noto Sans CJK TC",
    "DejaVu Sans",
]


def configure_matplotlib_chinese_fonts() -> list[str]:
    """Configure a portable Chinese font fallback stack for Matplotlib."""
    mpl.rcParams["font.sans-serif"] = CHINESE_FONT_FALLBACKS
    mpl.rcParams["axes.unicode_minus"] = False
    return CHINESE_FONT_FALLBACKS.copy()

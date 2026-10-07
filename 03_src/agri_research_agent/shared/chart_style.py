"""Presentation-only colors shared by research charts and comparison tables."""
from __future__ import annotations

import math

CURRENT_YEAR_COLOR = "#C1493F"
CURRENT_LINE_WIDTH = 3.4
HISTORY_LINE_WIDTH = 2.0
HISTORY_OPACITY = 0.55
MEAN_COLOR = "#667085"
GRID_COLOR = "#E1E5E9"

# International spread's Streamlit colorway, anchored to calendar years.
PALETTE_ANCHOR_YEAR = 2021
HISTORICAL_YEAR_COLORS = (
    "#0068C9", "#83C9FF", "#FF2B2B", "#FFABAB", "#29B09D",
    "#7DEFA1", "#FF8700", "#FFD16A", "#6D3FC0", "#D5DAE5",
)
ABOVE_MEAN_TEXT = "#A6342B"
ABOVE_MEAN_BACKGROUND = "#FCE8E6"
BELOW_MEAN_TEXT = "#174C91"
BELOW_MEAN_BACKGROUND = "#E5EFFB"


def historical_year_color(year: int) -> str:
    """Keep a historical year's color stable when selections change."""
    return HISTORICAL_YEAR_COLORS[(year - PALETTE_ANCHOR_YEAR) % len(HISTORICAL_YEAR_COLORS)]


def difference_cell_style(value: str | float | None) -> str:
    """Highlight signed differences without assigning good/bad semantics."""
    if value is None or value == "—":
        return ""
    number = float(value)
    if not math.isfinite(number) or number == 0:
        return ""
    color, background = ((ABOVE_MEAN_TEXT, ABOVE_MEAN_BACKGROUND) if number > 0
                         else (BELOW_MEAN_TEXT, BELOW_MEAN_BACKGROUND))
    return f"color: {color}; background-color: {background}; font-weight: bold;"

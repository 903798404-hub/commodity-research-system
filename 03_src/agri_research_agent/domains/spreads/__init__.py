"""Spread-domain models, parsing, calculations, and history rules."""

from .calculation import add_plot_value, calculate_spread
from .history import (
    FIVE_YEAR_MEAN_LABEL,
    FIVE_YEAR_MEAN_SMOOTH_WINDOW,
    HistoryView,
    SpreadHistorySummary,
    five_year_mean_seasons,
    is_non_season_name,
    latest_plot_value,
    latest_summary,
    prepare_history_view,
    season_sort_key,
)
from .models import (
    SpreadCalculation,
    SpreadDefinition,
    SpreadLeg,
    SpreadResult,
    SpreadStatus,
)
from .parsing import (
    BOARD_OPTIONS,
    ParsedSpreadName,
    classify_board,
    configured_spreads,
    definition_from_legacy,
    display_spread_name,
    parse_season,
    parse_spread_name,
    spread_sort_key,
)

__all__ = [
    "BOARD_OPTIONS",
    "FIVE_YEAR_MEAN_LABEL",
    "FIVE_YEAR_MEAN_SMOOTH_WINDOW",
    "HistoryView",
    "ParsedSpreadName",
    "SpreadCalculation",
    "SpreadDefinition",
    "SpreadHistorySummary",
    "SpreadLeg",
    "SpreadResult",
    "SpreadStatus",
    "add_plot_value",
    "calculate_spread",
    "classify_board",
    "configured_spreads",
    "definition_from_legacy",
    "display_spread_name",
    "five_year_mean_seasons",
    "is_non_season_name",
    "latest_plot_value",
    "latest_summary",
    "parse_season",
    "parse_spread_name",
    "prepare_history_view",
    "season_sort_key",
    "spread_sort_key",
]

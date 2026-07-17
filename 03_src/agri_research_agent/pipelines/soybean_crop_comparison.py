"""Build strict weekly soybean crop comparisons for the Streamlit workspace."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


HISTORICAL_WEEK_OFFSETS = (52, 104, 156, 208, 260)
CUMULATIVE_PROGRESS_METRICS = frozenset(
    {"PLANTED", "EMERGED", "BLOOMING", "SETTING_PODS", "HARVESTED"}
)
HISTORICAL_FALLBACK_DAYS = 7
MATCH_METHOD_EXACT = "exact"
MATCH_METHOD_PREVIOUS = "previous_within_7_days"
MATCH_METHOD_MISSING = "missing"
VALID_MATCH_METHODS = frozenset(
    {MATCH_METHOD_EXACT, MATCH_METHOD_PREVIOUS, MATCH_METHOD_MISSING}
)
DISPLAY_COLUMNS = [
    "地区",
    "最新进度（%）",
    "去年同期（%）",
    "五年均值（%）",
    "较去年同期（百分点）",
    "较五年均值（百分点）",
]
VALUE_COLUMNS = ["最新进度（%）", "去年同期（%）", "五年均值（%）"]
DELTA_COLUMNS = ["较去年同期（百分点）", "较五年均值（百分点）"]


@dataclass(frozen=True)
class MetricDefinition:
    """One configured metric shown in the workspace."""

    metric: str
    source: str
    tab_label: str
    display_name: str


@dataclass
class MetricComparison:
    """Comparison result for one metric and one independently selected week."""

    definition: MetricDefinition
    current_year: int
    baseline_week: pd.Timestamp | None
    latest_historical_week: pd.Timestamp | None
    rows: pd.DataFrame

    @property
    def previous_missing_count(self) -> int:
        if self.rows.empty:
            return 0
        return int(self.rows["previous_value_pct"].isna().sum())

    @property
    def five_year_missing_count(self) -> int:
        if self.rows.empty:
            return 0
        return int(self.rows["five_year_mean_pct"].isna().sum())


@dataclass(frozen=True)
class HistoricalMatch:
    """One auditable historical value match."""

    value_pct: float | None
    method: str


def load_display_config(path: str | Path) -> dict[str, Any]:
    """Load and validate the display-only state ordering configuration."""

    with Path(path).open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("Soybean crop display config must be a mapping")
    required_metadata = {
        "weight_role": "display_only",
        "weight_source": "user_provided",
        "weight_vintage": "unknown",
    }
    for key, expected in required_metadata.items():
        if config.get(key) != expected:
            raise ValueError(f"{key} must be {expected!r}")

    metrics = config.get("metrics")
    states = config.get("states")
    national = config.get("national")
    if not isinstance(metrics, list) or len(metrics) != 6:
        raise ValueError("Exactly six display metrics must be configured")
    if not isinstance(states, list) or len(states) != 18:
        raise ValueError("Exactly 18 default states must be configured")
    if not isinstance(national, dict) or national.get("region_name") != "US TOTAL":
        raise ValueError("The national row must be configured as US TOTAL")

    weights = [float(state["display_weight_pct"]) for state in states]
    if weights != sorted(weights, reverse=True):
        raise ValueError("States must be ordered by descending display weight")
    if len({state["region_name"] for state in states}) != len(states):
        raise ValueError("State region names must be unique")
    return config


def metric_definitions(config: dict[str, Any]) -> list[MetricDefinition]:
    """Return configured page metrics in tab order."""

    return [
        MetricDefinition(
            metric=str(item["metric"]),
            source=str(item["source"]),
            tab_label=str(item["tab_label"]),
            display_name=str(item["display_name"]),
        )
        for item in config["metrics"]
    ]


def determine_current_year(*frames: pd.DataFrame) -> int:
    """Use the maximum calendar year present anywhere in the processed inputs."""

    years = [
        int(value)
        for frame in frames
        if "calendar_year" in frame.columns
        for value in pd.to_numeric(frame["calendar_year"], errors="coerce").dropna()
    ]
    if not years:
        raise ValueError("No calendar year is available in processed crop data")
    return max(years)


def round_half_up(value: object) -> int | pd._libs.missing.NAType:
    """Round a non-negative percentage normally instead of truncating/bankers rounding."""

    if pd.isna(value):
        return pd.NA
    return int(Decimal(str(float(value))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _prepare_metric_rows(frame: pd.DataFrame, metric: str) -> pd.DataFrame:
    required = {
        "metric",
        "geography_level",
        "region_name",
        "calendar_year",
        "week_ending",
        "value_pct",
    }
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Processed crop data is missing columns: {sorted(missing)}")

    rows = frame.loc[frame["metric"].eq(metric), list(required)].copy()
    rows["week_ending"] = pd.to_datetime(rows["week_ending"], errors="coerce").dt.normalize()
    rows["calendar_year"] = pd.to_numeric(rows["calendar_year"], errors="coerce")
    rows["value_pct"] = pd.to_numeric(rows["value_pct"], errors="coerce")
    rows = rows.dropna(subset=["week_ending"])
    duplicate_keys = rows.duplicated(
        subset=["geography_level", "region_name", "week_ending"], keep=False
    )
    if duplicate_keys.any():
        sample = rows.loc[
            duplicate_keys, ["geography_level", "region_name", "week_ending"]
        ].head(3)
        raise ValueError(
            "Processed crop data contains duplicate comparison keys: "
            + sample.to_dict(orient="records").__repr__()
        )
    return rows


def _exact_value(
    rows: pd.DataFrame,
    geography_level: str,
    region_name: str,
    week_ending: pd.Timestamp,
) -> float | None:
    matched = rows.loc[
        rows["geography_level"].eq(geography_level)
        & rows["region_name"].eq(region_name)
        & rows["week_ending"].eq(week_ending),
        "value_pct",
    ]
    if matched.empty or pd.isna(matched.iloc[0]):
        return None
    return float(matched.iloc[0])


def _historical_value(
    rows: pd.DataFrame,
    geography_level: str,
    region_name: str,
    target_week: pd.Timestamp,
    allow_previous_fallback: bool,
) -> HistoricalMatch:
    """Match exact first, then the nearest prior official week within seven days."""

    region_rows = rows.loc[
        rows["geography_level"].eq(geography_level)
        & rows["region_name"].eq(region_name)
        & rows["calendar_year"].eq(target_week.year)
    ]
    exact = region_rows.loc[region_rows["week_ending"].eq(target_week)]
    if not exact.empty:
        value = exact.iloc[0]["value_pct"]
        if not pd.isna(value):
            return HistoricalMatch(float(value), MATCH_METHOD_EXACT)
        return HistoricalMatch(None, MATCH_METHOD_MISSING)

    if allow_previous_fallback:
        earliest_week = target_week - pd.Timedelta(days=HISTORICAL_FALLBACK_DAYS)
        previous = region_rows.loc[
            region_rows["week_ending"].lt(target_week)
            & region_rows["week_ending"].ge(earliest_week)
        ].sort_values("week_ending", ascending=False)
        if not previous.empty:
            value = previous.iloc[0]["value_pct"]
            if not pd.isna(value):
                return HistoricalMatch(float(value), MATCH_METHOD_PREVIOUS)

    return HistoricalMatch(None, MATCH_METHOD_MISSING)


def _display_region_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    national = config["national"]
    rows = [
        {
            "geography_level": "US",
            "region_name": national["region_name"],
            "display_region": national["display_name"],
            "display_weight_pct": None,
        }
    ]
    rows.extend(
        {
            "geography_level": "STATE",
            "region_name": state["region_name"],
            "display_region": (
                f'{state["display_name"]}（{float(state["display_weight_pct"]):.1f}%）'
            ),
            "display_weight_pct": float(state["display_weight_pct"]),
        }
        for state in config["states"]
    )
    return rows


def build_metric_comparison(
    frame: pd.DataFrame,
    definition: MetricDefinition,
    config: dict[str, Any],
    current_year: int,
) -> MetricComparison:
    """Build one metric table with controlled historical week matching."""

    metric_rows = _prepare_metric_rows(frame, definition.metric)
    national = config["national"]["region_name"]
    national_rows = metric_rows.loc[
        metric_rows["geography_level"].eq("US")
        & metric_rows["region_name"].eq(national)
    ]
    latest_historical_week = (
        national_rows["week_ending"].max() if not national_rows.empty else None
    )
    current_national = national_rows.loc[
        national_rows["calendar_year"].eq(current_year)
    ]
    if current_national.empty:
        return MetricComparison(
            definition=definition,
            current_year=current_year,
            baseline_week=None,
            latest_historical_week=latest_historical_week,
            rows=pd.DataFrame(),
        )

    baseline_week = pd.Timestamp(current_national["week_ending"].max()).normalize()
    historical_weeks = [
        baseline_week - pd.Timedelta(weeks=offset)
        for offset in HISTORICAL_WEEK_OFFSETS
    ]
    comparison_rows: list[dict[str, Any]] = []
    allow_previous_fallback = definition.metric in CUMULATIVE_PROGRESS_METRICS
    for sort_order, region in enumerate(_display_region_rows(config)):
        historical_matches = [
            _historical_value(
                metric_rows,
                region["geography_level"],
                region["region_name"],
                historical_week,
                allow_previous_fallback,
            )
            for historical_week in historical_weeks
        ]
        historical_values = [match.value_pct for match in historical_matches]
        historical_methods = tuple(match.method for match in historical_matches)
        if not set(historical_methods).issubset(VALID_MATCH_METHODS):
            raise ValueError("Unexpected historical match method")
        current_value = _exact_value(
            metric_rows,
            region["geography_level"],
            region["region_name"],
            baseline_week,
        )
        previous_value = historical_values[0]
        five_year_mean = (
            float(sum(historical_values) / len(historical_values))
            if all(value is not None for value in historical_values)
            else None
        )
        current_display = round_half_up(current_value)
        previous_display = round_half_up(previous_value)
        five_year_display = round_half_up(five_year_mean)
        change_previous = (
            current_display - previous_display
            if not pd.isna(current_display) and not pd.isna(previous_display)
            else pd.NA
        )
        change_five_year = (
            current_display - five_year_display
            if not pd.isna(current_display) and not pd.isna(five_year_display)
            else pd.NA
        )
        comparison_rows.append(
            {
                "sort_order": sort_order,
                "geography_level": region["geography_level"],
                "region_name": region["region_name"],
                "display_region": region["display_region"],
                "display_weight_pct": region["display_weight_pct"],
                "baseline_week": baseline_week,
                "current_value_pct": current_value,
                "previous_value_pct": previous_value,
                "five_year_mean_pct": five_year_mean,
                "historical_match_count": sum(
                    method != MATCH_METHOD_MISSING for method in historical_methods
                ),
                "last_year_match_method": historical_methods[0],
                "five_year_match_methods": historical_methods,
                "five_year_fallback_count": historical_methods.count(
                    MATCH_METHOD_PREVIOUS
                ),
                "current_display": current_display,
                "previous_display": previous_display,
                "five_year_display": five_year_display,
                "change_vs_previous_display": change_previous,
                "change_vs_five_year_display": change_five_year,
            }
        )
    result_rows = pd.DataFrame(comparison_rows)
    integer_columns = [
        "current_display",
        "previous_display",
        "five_year_display",
        "change_vs_previous_display",
        "change_vs_five_year_display",
    ]
    result_rows[integer_columns] = result_rows[integer_columns].astype("Int64")
    return MetricComparison(
        definition=definition,
        current_year=current_year,
        baseline_week=baseline_week,
        latest_historical_week=latest_historical_week,
        rows=result_rows,
    )


def build_dashboard_comparisons(
    progress: pd.DataFrame,
    condition: pd.DataFrame,
    config: dict[str, Any],
) -> list[MetricComparison]:
    """Build all six page tabs while selecting each metric's own national week."""

    current_year = determine_current_year(progress, condition)
    sources = {"progress": progress, "condition": condition}
    comparisons = []
    for definition in metric_definitions(config):
        if definition.source not in sources:
            raise ValueError(f"Unknown crop metric source: {definition.source}")
        comparisons.append(
            build_metric_comparison(
                sources[definition.source], definition, config, current_year
            )
        )
    return comparisons


def comparison_display_table(comparison: MetricComparison) -> pd.DataFrame:
    """Return the Chinese integer table rendered by Streamlit."""

    if comparison.rows.empty:
        return pd.DataFrame(columns=DISPLAY_COLUMNS)
    display = comparison.rows[
        [
            "display_region",
            "current_display",
            "previous_display",
            "five_year_display",
            "change_vs_previous_display",
            "change_vs_five_year_display",
        ]
    ].copy()
    display.columns = DISPLAY_COLUMNS
    return display


def format_integer_or_dash(value: object) -> str:
    """Format nullable display values without interpolation or implicit zeroes."""

    if pd.isna(value):
        return "—"
    return str(int(value))


def delta_cell_style(value: object) -> str:
    """Apply the page's positive, negative, zero, and missing delta colors."""

    if pd.isna(value):
        return "background-color: #f8fafc; color: #64748b;"
    numeric = float(value)
    if numeric > 0:
        return "background-color: #dcfce7; color: #166534;"
    if numeric < 0:
        return "background-color: #fee2e2; color: #991b1b;"
    return "background-color: #ffffff; color: #111827;"


def style_comparison_table(comparison: MetricComparison) -> pd.io.formats.style.Styler:
    """Create the compact styled table used by the main workspace."""

    display = comparison_display_table(comparison)

    def style_national_row(row: pd.Series) -> list[str]:
        return ["font-weight: 700;" if row.name == 0 else "" for _ in row]

    return (
        display.style.format(format_integer_or_dash, subset=VALUE_COLUMNS + DELTA_COLUMNS)
        .map(delta_cell_style, subset=DELTA_COLUMNS)
        .apply(style_national_row, axis=1)
        .set_properties(subset=VALUE_COLUMNS, **{"color": "#111827"})
        .hide(axis="index")
        .set_table_attributes(
            'class="soybean-comparison-table" '
            'style="width:100%; table-layout:fixed; border-collapse:collapse; '
            'font-size:0.86rem;"'
        )
        .set_table_styles(
            [
                {
                    "selector": "th",
                    "props": [
                        ("background-color", "#dbeafe"),
                        ("color", "#0f172a"),
                        ("font-weight", "700"),
                        ("text-align", "center"),
                        ("white-space", "normal"),
                        ("line-height", "1.25"),
                        ("padding", "0.45rem 0.3rem"),
                        ("border", "1px solid #cbd5e1"),
                    ],
                },
                {
                    "selector": "td",
                    "props": [
                        ("padding", "0.35rem 0.3rem"),
                        ("white-space", "normal"),
                        ("overflow-wrap", "anywhere"),
                        ("text-align", "center"),
                        ("border", "1px solid #e2e8f0"),
                    ],
                },
                {
                    "selector": "th.col0, td.col0",
                    "props": [("width", "22%"), ("text-align", "left")],
                },
                {
                    "selector": "th.col1, td.col1, th.col2, td.col2, th.col3, td.col3",
                    "props": [("width", "13%")],
                },
                {
                    "selector": "th.col4, td.col4, th.col5, td.col5",
                    "props": [("width", "19.5%")],
                },
            ]
        )
    )

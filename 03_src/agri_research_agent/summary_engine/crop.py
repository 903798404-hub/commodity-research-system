from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from agri_research_agent.pipelines.soybean_crop_comparison import (
    MATCH_METHOD_PREVIOUS,
    MetricComparison,
    MetricDefinition,
    build_metric_comparison,
    determine_current_year,
    load_display_config,
    round_half_up,
)

from .rules import freshness_status, load_summary_rules
from .schema import Summary


DEFAULT_DISPLAY_CONFIG = (
    Path(__file__).resolve().parents[3]
    / "02_configs"
    / "soybean_crop_progress_display.yaml"
)


def _display_names(display_config: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, str]:
    names = {
        str(item["metric"]): str(item["tab_label"])
        for item in display_config["metrics"]
    }
    names.update(
        {
            str(metric): str(label)
            for metric, label in rules["soybean_crop"].get(
                "additional_display_names", {}
            ).items()
        }
    )
    return names


def _source_for_metric(metric: str) -> str:
    return "condition" if metric == "GOOD_EXCELLENT" else "progress"


def _previous_current_year_value(
    frame: pd.DataFrame,
    *,
    metric: str,
    geography_level: str,
    region_name: str,
    current_year: int,
    baseline_week: pd.Timestamp,
) -> tuple[pd.Timestamp | None, float | None]:
    rows = frame.loc[
        frame["metric"].eq(metric)
        & frame["geography_level"].eq(geography_level)
        & frame["region_name"].eq(region_name)
        & pd.to_numeric(frame["calendar_year"], errors="coerce").eq(current_year),
        ["week_ending", "value_pct"],
    ].copy()
    rows["week_ending"] = pd.to_datetime(rows["week_ending"], errors="coerce").dt.normalize()
    rows["value_pct"] = pd.to_numeric(rows["value_pct"], errors="coerce")
    rows = rows.loc[rows["week_ending"].lt(baseline_week)].dropna(
        subset=["week_ending", "value_pct"]
    )
    if rows.empty:
        return None, None
    previous = rows.sort_values("week_ending").iloc[-1]
    return pd.Timestamp(previous["week_ending"]), float(previous["value_pct"])


def _display_number(value: object) -> int | None:
    rounded = round_half_up(value)
    return None if pd.isna(rounded) else int(rounded)


def _difference(current: int | None, comparison: int | None) -> int | None:
    return None if current is None or comparison is None else current - comparison


def _history_direction(
    difference: int | None, *, condition: bool, threshold: float
) -> str:
    if difference is None:
        return "样本不足"
    if difference >= threshold:
        return "高于" if condition else "领先"
    if difference <= -threshold:
        return "低于" if condition else "落后"
    return "接近"


def _metric_fact(
    comparison: MetricComparison,
    source: pd.DataFrame,
    *,
    threshold: float,
) -> dict[str, Any] | None:
    if comparison.baseline_week is None or comparison.rows.empty:
        return None
    national = comparison.rows.loc[
        comparison.rows["geography_level"].eq("US")
        & comparison.rows["region_name"].eq("US TOTAL")
    ]
    if national.empty or pd.isna(national.iloc[0]["current_value_pct"]):
        return None
    row = national.iloc[0]
    previous_week, previous_value = _previous_current_year_value(
        source,
        metric=comparison.definition.metric,
        geography_level="US",
        region_name="US TOTAL",
        current_year=comparison.current_year,
        baseline_week=comparison.baseline_week,
    )
    current = _display_number(row["current_value_pct"])
    previous = _display_number(previous_value)
    last_year = _display_number(row["previous_value_pct"])
    five_year = _display_number(row["five_year_mean_pct"])
    versus_last_year = _difference(current, last_year)
    versus_five_year = _difference(current, five_year)
    condition = comparison.definition.metric == "GOOD_EXCELLENT"
    return {
        "metric": comparison.definition.metric,
        "display_name": comparison.definition.tab_label,
        "metric_kind": "condition" if condition else "progress",
        "week_ending": comparison.baseline_week.date().isoformat(),
        "current_pct": current,
        "previous_week": None if previous_week is None else previous_week.date().isoformat(),
        "previous_week_pct": previous,
        "week_change_pct_points": _difference(current, previous),
        "last_year_pct": last_year,
        "difference_vs_last_year_pct_points": versus_last_year,
        "five_year_mean_pct": five_year,
        "five_year_mean_raw_pct": (
            None if pd.isna(row["five_year_mean_pct"]) else float(row["five_year_mean_pct"])
        ),
        "difference_vs_five_year_pct_points": versus_five_year,
        "comparison_week": (
            comparison.baseline_week - pd.Timedelta(weeks=52)
        ).date().isoformat(),
        "fallback_used": row["last_year_match_method"] == MATCH_METHOD_PREVIOUS,
        "last_year_match_method": str(row["last_year_match_method"]),
        "five_year_sample_count": int(row["historical_match_count"]),
        "five_year_fallback_count": int(row["five_year_fallback_count"]),
        "direction_vs_last_year": _history_direction(
            versus_last_year, condition=condition, threshold=threshold
        ),
        "direction_vs_five_year": _history_direction(
            versus_five_year, condition=condition, threshold=threshold
        ),
    }


def _current_stage(
    active_metrics: set[str], crop_rules: Mapping[str, Any]
) -> Mapping[str, Any]:
    for stage in crop_rules["season_stages"]:
        if any(str(metric) in active_metrics for metric in stage["trigger_metrics"]):
            return stage
    return crop_rules["season_stages"][-1]


def _is_meaningful_focus(item: Mapping[str, Any], crop_rules: Mapping[str, Any]) -> bool:
    if item["metric"] == "GOOD_EXCELLENT":
        return True
    change_threshold = float(crop_rules["focus_change_pct_points"])
    values = (
        item.get("week_change_pct_points"),
        item.get("difference_vs_last_year_pct_points"),
        item.get("difference_vs_five_year_pct_points"),
    )
    return any(value is not None and abs(float(value)) >= change_threshold for value in values)


def _select_focus_metrics(
    metrics: list[dict[str, Any]],
    stage: Mapping[str, Any],
    crop_rules: Mapping[str, Any],
) -> list[dict[str, Any]]:
    by_metric = {str(item["metric"]): item for item in metrics}
    ordered = [
        by_metric[str(metric)]
        for metric in stage["priority_metrics"]
        if str(metric) in by_metric
    ]
    meaningful = [item for item in ordered if _is_meaningful_focus(item, crop_rules)]
    target = min(int(crop_rules["focus_target"]), int(crop_rules["max_focus_metrics"]))
    selected = meaningful[:target]
    if len(selected) < min(2, len(ordered)):
        selected_ids = {item["metric"] for item in selected}
        selected.extend(
            item
            for item in ordered
            if item["metric"] not in selected_ids
        )
    return selected[: int(crop_rules["max_focus_metrics"])]


def _state_anomalies(
    comparison: MetricComparison | None,
    condition: pd.DataFrame,
    display_config: Mapping[str, Any],
    crop_rules: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if comparison is None or comparison.baseline_week is None or comparison.rows.empty:
        return []
    names = {
        str(item["region_name"]): str(item["display_name"])
        for item in display_config["states"]
    }
    threshold = float(crop_rules["state_change_pct_points"])
    rows: list[dict[str, Any]] = []
    for _, current in comparison.rows.loc[
        comparison.rows["geography_level"].eq("STATE")
    ].iterrows():
        region_name = str(current["region_name"])
        if region_name not in names or pd.isna(current["current_value_pct"]):
            continue
        previous_week, previous_value = _previous_current_year_value(
            condition,
            metric="GOOD_EXCELLENT",
            geography_level="STATE",
            region_name=region_name,
            current_year=comparison.current_year,
            baseline_week=comparison.baseline_week,
        )
        current_pct = _display_number(current["current_value_pct"])
        previous_pct = _display_number(previous_value)
        last_year_pct = _display_number(current["previous_value_pct"])
        week_change = _difference(current_pct, previous_pct)
        if week_change is None or abs(week_change) < threshold:
            continue
        rows.append(
            {
                "state": names[region_name],
                "week_ending": comparison.baseline_week.date().isoformat(),
                "current_pct": current_pct,
                "previous_week": (
                    None if previous_week is None else previous_week.date().isoformat()
                ),
                "week_change_pct_points": week_change,
                "last_year_pct": last_year_pct,
                "difference_vs_last_year_pct_points": _difference(
                    current_pct, last_year_pct
                ),
            }
        )
    rows.sort(key=lambda item: (-abs(item["week_change_pct_points"]), item["state"]))
    return rows[: int(crop_rules["max_state_anomalies"])]


def _fmt_number(value: int | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value:g}{suffix}"


def _week_change_text(item: Mapping[str, Any]) -> str:
    value = item.get("week_change_pct_points")
    if value is None:
        return "无可比上周"
    if value == 0:
        return "持平"
    direction = "回升" if item.get("metric_kind") == "condition" and value > 0 else "上升" if value > 0 else "下降"
    return f"**{direction}** {abs(value):g}个百分点"


def _history_change_text(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if value is None:
        return "历史样本不足"
    direction_key = (
        "direction_vs_last_year"
        if key == "difference_vs_last_year_pct_points"
        else "direction_vs_five_year"
    )
    direction = str(item[direction_key])
    if direction == "接近":
        return f"**接近**（{value:+g}个百分点）"
    return f"**{direction}** {abs(value):g}个百分点"


def _judgement_text(item: Mapping[str, Any]) -> str:
    last_year = str(item["direction_vs_last_year"])
    five_year = str(item["direction_vs_five_year"])
    if item["metric_kind"] == "condition":
        trend = _week_change_text(item)
        if last_year == five_year and last_year != "样本不足":
            level = f"当前 **{last_year}历史**"
        else:
            level = f"较去年 **{last_year}**、较5年 **{five_year}**"
        return f"{level}，本周{trend}"
    if last_year == five_year and last_year not in {"样本不足", "接近"}:
        return f"进度 **{last_year}**"
    if last_year == five_year == "接近":
        return "进度 **接近** 历史"
    return f"较去年 **{last_year}**、较5年 **{five_year}**"


def _metric_history_clause(item: Mapping[str, Any]) -> str:
    name = str(item["display_name"])
    last_year = str(item["direction_vs_last_year"])
    five_year = str(item["direction_vs_five_year"])
    if last_year == five_year == "领先":
        return f"{name}较去年同期及5年平均均 **领先**"
    if last_year == five_year == "落后":
        return f"{name}较去年同期及5年平均均 **落后**"
    if last_year == five_year == "接近":
        return f"{name}与去年同期及5年平均 **接近**"
    return f"{name}较去年同期 **{last_year}**、较5年平均 **{five_year}**"


def _overall_progress_direction(core: list[Mapping[str, Any]]) -> str:
    directions = [
        item[key]
        for item in core
        for key in ("direction_vs_last_year", "direction_vs_five_year")
        if item[key] != "样本不足"
    ]
    if directions and all(value in {"领先", "接近"} for value in directions) and "领先" in directions:
        return "偏快"
    if directions and all(value in {"落后", "接近"} for value in directions) and "落后" in directions:
        return "偏慢"
    return "接近历史水平"


def _comprehensive_text(
    metrics: list[dict[str, Any]],
    focus: list[dict[str, Any]],
    stage: Mapping[str, Any],
) -> tuple[str, str]:
    by_metric = {item["metric"]: item for item in metrics}
    core = [
        by_metric[str(metric)]
        for metric in stage["core_progress_metrics"]
        if str(metric) in by_metric
    ]
    progress_direction = _overall_progress_direction(core)
    progress_sentence = ""
    if core:
        clauses = "，".join(_metric_history_clause(item) for item in core[:2])
        displayed_direction = (
            "**接近**历史水平"
            if progress_direction == "接近历史水平"
            else f"**{progress_direction}**"
        )
        progress_sentence = f"当前美豆{clauses}，生育进度整体{displayed_direction}。"

    condition = by_metric.get("GOOD_EXCELLENT")
    condition_sentence = ""
    if condition:
        week = _week_change_text(condition)
        last_difference = condition.get("difference_vs_last_year_pct_points")
        five_difference = condition.get("difference_vs_five_year_pct_points")
        last_direction = str(condition["direction_vs_last_year"])
        five_direction = str(condition["direction_vs_five_year"])
        last_year = (
            "相对去年同期历史样本不足"
            if last_difference is None
            else f"与去年同期 **接近**（{last_difference:+g}个百分点）"
            if last_direction == "接近"
            else f"**{last_direction}** 去年同期{abs(last_difference):g}个百分点"
        )
        five_year = (
            "相对5年平均历史样本不足"
            if five_difference is None
            else f"与5年平均 **接近**（{five_difference:+g}个百分点）"
            if five_direction == "接近"
            else f"**{five_direction}** 5年平均{abs(five_difference):g}个百分点"
        )
        condition_sentence = f"优良率较上周{week}，当前{last_year}、{five_year}。"

    attention_parts: list[str] = []
    if condition and condition.get("week_change_pct_points") is not None:
        direction = "回升" if condition["week_change_pct_points"] > 0 else "下降" if condition["week_change_pct_points"] < 0 else "持平"
        attention_parts.append(f"优良率本周 **{direction}**")
    notable_progress = next(
        (
            item
            for item in focus
            if item["metric_kind"] == "progress"
            and (
                item["direction_vs_last_year"] in {"领先", "落后"}
                or item["direction_vs_five_year"] in {"领先", "落后"}
            )
        ),
        None,
    )
    if notable_progress:
        attention_parts.append(f'{notable_progress["display_name"]}相对历史水平的差异')
    attention_sentence = (
        "当前重点关注" + "，以及".join(attention_parts[:2]) + "。"
        if attention_parts
        else ""
    )
    return progress_sentence + condition_sentence + attention_sentence, progress_direction


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _detail_text(
    comprehensive: str,
    focus: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    state_anomalies: list[dict[str, Any]],
) -> str:
    focus_rows = [
        [
            item["display_name"],
            _fmt_number(item["current_pct"], "%"),
            _week_change_text(item),
            _history_change_text(item, "difference_vs_last_year_pct_points"),
            _history_change_text(item, "difference_vs_five_year_pct_points"),
            _judgement_text(item),
        ]
        for item in focus
    ]
    metric_rows = [
        [
            item["display_name"],
            item["week_ending"],
            _fmt_number(item["current_pct"], "%"),
            _week_change_text(item),
            _fmt_number(item["last_year_pct"], "%"),
            _history_change_text(item, "difference_vs_last_year_pct_points"),
            _fmt_number(item["five_year_mean_pct"], "%") if item["five_year_mean_pct"] is not None else "历史样本不足",
            _history_change_text(item, "difference_vs_five_year_pct_points"),
        ]
        for item in metrics
    ]
    sections = [
        f"### 综合\n{comprehensive}",
        "### 重点变化\n"
        + _markdown_table(
            ["指标", "最新值", "较上周", "较去年同期", "较5年均值", "当前判断"],
            focus_rows,
        ),
        "### 当前有效指标\n"
        + _markdown_table(
            ["指标", "数据周", "当前值", "较上周", "去年同期", "较去年", "5年均值", "较5年"],
            metric_rows,
        ),
    ]
    if state_anomalies:
        state_rows = [
            [
                item["state"],
                _fmt_number(item["current_pct"], "%"),
                _week_change_text({**item, "metric_kind": "condition"}),
                _history_change_text(
                    {
                        **item,
                        "metric_kind": "condition",
                        "direction_vs_last_year": _history_direction(
                            item["difference_vs_last_year_pct_points"],
                            condition=True,
                            threshold=0.5,
                        ),
                    },
                    "difference_vs_last_year_pct_points",
                ),
            ]
            for item in state_anomalies
        ]
        sections.append(
            "### 州级变化\n以下仅列州级优良率变化事实。\n\n"
            + _markdown_table(["州", "当前优良率", "较上周", "较去年同期"], state_rows)
        )
    return "\n\n".join(sections)


def _short_text(
    progress_direction: str,
    metrics: list[dict[str, Any]],
    stage: Mapping[str, Any],
    state_anomalies: list[dict[str, Any]],
) -> str:
    by_metric = {item["metric"]: item for item in metrics}
    core = [
        by_metric[str(metric)]
        for metric in stage["core_progress_metrics"]
        if str(metric) in by_metric
    ]
    clauses = "，".join(_metric_history_clause(item) for item in core[:2])
    displayed_direction = (
        "**接近**历史水平"
        if progress_direction == "接近历史水平"
        else f"**{progress_direction}**"
    )
    text = f"美豆生育进度整体{displayed_direction}"
    if clauses:
        text += f"，{clauses}"
    condition = by_metric.get("GOOD_EXCELLENT")
    if condition:
        week = _week_change_text(condition)
        last_year = condition["direction_vs_last_year"]
        five_year = condition["direction_vs_five_year"]
        last_phrase = f"**{last_year}**去年同期" if last_year != "接近" else "与去年同期**接近**"
        five_phrase = f"**{five_year}**5年平均" if five_year != "接近" else "与5年平均**接近**"
        text += f"；优良率本周{week}，{last_phrase}、{five_phrase}"
    declines = [
        item["state"]
        for item in state_anomalies
        if item["week_change_pct_points"] < 0
    ]
    if declines:
        names = "、".join(declines[:3])
        text += f"；{names}{'等州' if len(declines) > 3 else ''}本周优良率**下降**"
    return text + "。"


def build_crop_summary(
    progress: pd.DataFrame,
    condition: pd.DataFrame,
    *,
    source_identity: Mapping[str, Any],
    generated_at: datetime | None = None,
    display_config: Mapping[str, Any] | None = None,
) -> Summary:
    rules = load_summary_rules()
    crop_rules = rules["soybean_crop"]
    config = dict(display_config) if display_config is not None else load_display_config(DEFAULT_DISPLAY_CONFIG)
    current_year = determine_current_year(progress, condition)
    names = _display_names(config, rules)
    sources = {"progress": progress, "condition": condition}
    comparisons: dict[str, MetricComparison] = {}
    metrics: list[dict[str, Any]] = []
    threshold = float(crop_rules["history_direction_pct_points"])
    for metric in crop_rules["metrics"]:
        metric = str(metric)
        source_name = _source_for_metric(metric)
        definition = MetricDefinition(metric, source_name, names[metric], names[metric])
        comparison = build_metric_comparison(
            sources[source_name], definition, config, current_year
        )
        comparisons[metric] = comparison
        fact = _metric_fact(comparison, sources[source_name], threshold=threshold)
        if fact is not None:
            metrics.append(fact)

    active_metrics = {str(item["metric"]) for item in metrics}
    stage = _current_stage(active_metrics, crop_rules)
    focus = _select_focus_metrics(metrics, stage, crop_rules)
    state_anomalies = _state_anomalies(
        comparisons.get("GOOD_EXCELLENT"), condition, config, crop_rules
    )
    comprehensive, progress_direction = _comprehensive_text(metrics, focus, stage)
    detail = _detail_text(comprehensive, focus, metrics, state_anomalies)
    short = _short_text(progress_direction, metrics, stage, state_anomalies)
    weeks = {str(item["week_ending"]) for item in metrics}
    latest = max(weeks, default=None)
    data_time_label = (
        f"数据更新至：{latest}"
        if len(weeks) <= 1
        else "数据更新至：各指标最新有效周（详见“当前有效指标”）"
    )
    facts = {
        "current_year": current_year,
        "current_stage": str(stage["name"]),
        "data_time_label": data_time_label,
        "metrics": metrics,
        "focus_metrics": focus,
        "state_anomalies": state_anomalies,
        "state_weights_role": "display_only",
        "good_excellent_definition": "GOOD + EXCELLENT",
    }
    return Summary.create(
        module="soybean_crop",
        source_dataset="soybean_crop_progress",
        source_identity=source_identity,
        source_date=latest,
        comparison_identity={
            "current_year": current_year,
            "method": "metric_own_latest_week",
            "progress_history_match": "exact_then_previous_within_7_days",
            "condition_history_match": "exact_only",
        },
        generated_at=generated_at,
        calculation_version=rules["calculation_version"],
        rule_version=rules["rule_version"],
        freshness_status=freshness_status("soybean_crop", latest, rules),
        facts=facts,
        classifications=[str(stage["name"]), progress_direction],
        headline="美豆种植生长最新变化",
        detail_text=detail,
        short_text=short,
    )

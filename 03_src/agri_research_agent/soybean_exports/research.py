from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .common import PipelineError, SCHEMA_VERSION, sha256_file
from .fas import build_fas_research_view, fas_paths, validate_fas_stable
from .fgis import build_fgis_research_view, fgis_paths, validate_fgis_stable


PSD_COMMODITY_CODE = "2222000"
PSD_COMMODITY = "Oilseed, Soybean"
PSD_COUNTRY_CODE = "US"
PSD_COUNTRY = "United States"
PSD_EXPORT_ROW = "出口量"
SALES_PROGRESS_NULL_REASON = "年度出口预测暂不可用"


def read_usda_psd_soybean_exports_mt(
    usda_project_root: Path, *, target_market_year_end: int
) -> dict[str, Any]:
    data_root = Path(usda_project_root) / "public" / "data"
    version_path = data_root / "report_version.json"
    if not version_path.is_file():
        raise PipelineError("USDA report_version.json is missing")
    try:
        version = json.loads(version_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError("USDA report_version.json is invalid UTF-8 JSON") from exc
    report_month = version.get("currentReportMonth")
    if not isinstance(report_month, str) or len(report_month) != 7:
        raise PipelineError("USDA currentReportMonth is missing or invalid")
    relative = Path("matrix") / "2222000_US.json"
    snapshot_path = data_root / "snapshots" / "usda_psd" / report_month / relative
    current_path = data_root / relative
    if not snapshot_path.is_file() or not current_path.is_file():
        raise PipelineError("USDA current or versioned soybean snapshot is missing")
    snapshot_sha = sha256_file(snapshot_path)
    current_sha = sha256_file(current_path)
    if snapshot_sha != current_sha:
        raise PipelineError("USDA current soybean matrix does not match currentReportMonth snapshot")
    try:
        matrix = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError("USDA soybean snapshot is invalid UTF-8 JSON") from exc
    identity = (
        matrix.get("commodityCode"),
        matrix.get("commodity"),
        matrix.get("countryCode"),
        matrix.get("country"),
    )
    expected = (PSD_COMMODITY_CODE, PSD_COMMODITY, PSD_COUNTRY_CODE, PSD_COUNTRY)
    if identity != expected:
        raise PipelineError(f"USDA soybean snapshot identity mismatch: {identity}")
    years = matrix.get("years")
    rows = matrix.get("rows")
    if not isinstance(years, list) or not isinstance(rows, list):
        raise PipelineError("USDA soybean snapshot years or rows are missing")
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == PSD_EXPORT_ROW]
    if len(matches) != 1 or not isinstance(matches[0].get("values"), list):
        raise PipelineError("USDA soybean Exports row is missing or not unique")
    market_year_start = int(target_market_year_end) - 1
    matching_indices = [index for index, year in enumerate(years) if int(year) == market_year_start]
    if len(matching_indices) != 1:
        raise PipelineError("USDA soybean target market year is missing or not unique")
    index = matching_indices[0]
    values = matches[0]["values"]
    if index >= len(values):
        raise PipelineError("USDA soybean Exports values do not align with years")
    try:
        exports_wan_mt = float(values[index])
    except (TypeError, ValueError) as exc:
        raise PipelineError("USDA soybean Exports value is not numeric") from exc
    if not pd.notna(exports_wan_mt) or exports_wan_mt <= 0:
        raise PipelineError("USDA soybean Exports value must be positive")
    return {
        "source": "usda_fas_psd",
        "report_month": report_month,
        "target_market_year_end": int(target_market_year_end),
        "market_year_start": market_year_start,
        "exports_mt": int(round(exports_wan_mt * 10_000)),
        "normalized_source_unit": "10,000 MT",
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": snapshot_sha,
    }


def _optional_sales_progress(
    usda_project_root: Path,
    *,
    target_market_year_end: int,
    commitments_mt: int,
) -> tuple[float | None, dict[str, Any] | None, str | None, str | None]:
    """Calculate sales progress without making PS&D a page availability gate."""

    try:
        denominator = read_usda_psd_soybean_exports_mt(
            Path(usda_project_root),
            target_market_year_end=target_market_year_end,
        )
    except Exception as exc:
        return None, None, SALES_PROGRESS_NULL_REASON, str(exc)
    return (
        commitments_mt / denominator["exports_mt"] * 100,
        denominator,
        None,
        None,
    )


def build_soybean_export_research_payload(
    *,
    fgis_stable: pd.DataFrame,
    fas_stable: pd.DataFrame,
    usda_project_root: Path,
) -> dict[str, Any]:
    validate_fgis_stable(fgis_stable)
    validate_fas_stable(fas_stable)
    fgis = build_fgis_research_view(fgis_stable)
    fas = build_fas_research_view(fas_stable)
    latest_fgis = fgis.sort_values(["market_year_end", "my_week"]).iloc[-1]
    previous_fgis = fgis.loc[
        fgis["market_year_end"].eq(int(latest_fgis["market_year_end"]) - 1)
        & fgis["my_week"].eq(int(latest_fgis["my_week"]))
    ]
    cumulative_yoy = None
    previous_cumulative = None
    if len(previous_fgis) == 1:
        previous_cumulative = int(previous_fgis.iloc[0]["world_cumulative_mt"])
        if previous_cumulative != 0:
            cumulative_yoy = (
                (int(latest_fgis["world_cumulative_mt"]) - previous_cumulative)
                / abs(previous_cumulative)
                * 100
            )

    latest_fas = fas.sort_values(["report_market_year_end", "report_week"]).iloc[-1]
    target_year = int(latest_fas["current_target_market_year_end"])
    commitments = int(latest_fas["world_current_my_total_commitment_mt"])
    sales_progress, denominator, sales_progress_null_reason, _psd_error = (
        _optional_sales_progress(
            usda_project_root,
            target_market_year_end=target_year,
            commitments_mt=commitments,
        )
    )
    fgis_fact = {
        "market_year_end": int(latest_fgis["market_year_end"]),
        "market_year_label": str(latest_fgis["market_year_label"]),
        "latest_week": latest_fgis["week_ending_date"].date().isoformat(),
        "my_week": int(latest_fgis["my_week"]),
        "weekly_world_mt": int(latest_fgis["world_weekly_mt"]),
        "cumulative_world_mt": int(latest_fgis["world_cumulative_mt"]),
        "weekly_china_mt": int(latest_fgis["china_weekly_mt"]),
        "weekly_non_china_mt": int(latest_fgis["non_china_weekly_mt"]),
        "china_share_pct": _optional_float(latest_fgis["china_share_pct"]),
        "previous_my_same_week_cumulative_mt": previous_cumulative,
        "cumulative_yoy_pct": cumulative_yoy,
    }
    fas_current = {
        "report_market_year_end": int(latest_fas["report_market_year_end"]),
        "target_market_year_end": target_year,
        "report_week": int(latest_fas["report_week"]),
        "latest_week": latest_fas["week_ending_date"].date().isoformat(),
        "world_weekly_net_sales_mt": int(latest_fas["world_current_my_net_sales_mt"]),
        "china_weekly_net_sales_mt": int(latest_fas["china_current_my_net_sales_mt"]),
        "non_china_weekly_net_sales_mt": int(latest_fas["non_china_current_my_net_sales_mt"]),
        "world_total_commitments_mt": commitments,
        "china_total_commitments_mt": int(latest_fas["china_current_my_total_commitment_mt"]),
        "non_china_total_commitments_mt": int(latest_fas["non_china_current_my_total_commitment_mt"]),
        "world_accumulated_exports_mt": int(latest_fas["world_accumulated_exports_mt"]),
        "world_outstanding_sales_mt": int(latest_fas["world_outstanding_sales_mt"]),
        "sales_progress_pct": sales_progress,
        "sales_progress_denominator": denominator,
        "sales_progress_null_reason": sales_progress_null_reason,
    }
    fas_next = {
        "report_market_year_end": int(latest_fas["report_market_year_end"]),
        "target_market_year_end": int(latest_fas["next_target_market_year_end"]),
        "report_week": int(latest_fas["report_week"]),
        "latest_week": latest_fas["week_ending_date"].date().isoformat(),
        "world_weekly_net_sales_mt": int(latest_fas["world_next_my_net_sales_mt"]),
        "china_weekly_net_sales_mt": int(latest_fas["china_next_my_net_sales_mt"]),
        "non_china_weekly_net_sales_mt": int(latest_fas["non_china_next_my_net_sales_mt"]),
        "world_total_presales_mt": int(latest_fas["world_next_my_outstanding_sales_mt"]),
        "china_total_presales_mt": int(latest_fas["china_next_my_outstanding_sales_mt"]),
        "non_china_total_presales_mt": int(latest_fas["non_china_next_my_outstanding_sales_mt"]),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "fgis": fgis_fact,
        "fas_current": fas_current,
        "fas_next": fas_next,
        "kpis": {
            "cumulative_export_inspections_yoy_pct": cumulative_yoy,
            "current_my_sales_progress_pct": sales_progress,
            "next_my_total_sales_mt": fas_next["world_total_presales_mt"],
            "china_next_my_total_purchases_mt": fas_next["china_total_presales_mt"],
        },
    }


def _optional_float(value: Any) -> float | None:
    return float(value) if pd.notna(value) else None


def load_soybean_export_page_payload(
    runtime_root: Path, *, usda_project_root: Path
) -> dict[str, Any]:
    """Read both published sources independently for the thin Streamlit consumer."""

    root = Path(runtime_root)
    fgis, fgis_manifest, fgis_status, fgis_error = _load_published_source(
        fgis_paths(root, "page-read"), validate_fgis_stable
    )
    fas, fas_manifest, fas_status, fas_error = _load_published_source(
        fas_paths(root, "page-read"), validate_fas_stable
    )
    return build_soybean_export_page_payload(
        fgis_stable=fgis,
        fas_stable=fas,
        usda_project_root=Path(usda_project_root),
        fgis_manifest=fgis_manifest,
        fas_manifest=fas_manifest,
        fgis_status=fgis_status,
        fas_status=fas_status,
        fgis_error=fgis_error,
        fas_error=fas_error,
    )


def build_soybean_export_page_payload(
    *,
    fgis_stable: pd.DataFrame | None,
    fas_stable: pd.DataFrame | None,
    usda_project_root: Path,
    fgis_manifest: Mapping[str, Any] | None = None,
    fas_manifest: Mapping[str, Any] | None = None,
    fgis_status: Mapping[str, Any] | None = None,
    fas_status: Mapping[str, Any] | None = None,
    fgis_error: str | None = None,
    fas_error: str | None = None,
) -> dict[str, Any]:
    """Build page-ready facts without introducing presentation or business formulas in UI code."""

    fgis_section: dict[str, Any] | None = None
    fas_section: dict[str, Any] | None = None
    errors: dict[str, str | None] = {"fgis": fgis_error, "fas": fas_error, "psd": None}

    if fgis_stable is not None and fgis_error is None:
        try:
            fgis_section = _build_fgis_page_section(
                fgis_stable,
                manifest=fgis_manifest or {},
                status=fgis_status or {},
            )
        except Exception as exc:
            errors["fgis"] = str(exc)

    if fas_stable is not None and fas_error is None:
        try:
            fas_section = _build_fas_page_section(
                fas_stable,
                manifest=fas_manifest or {},
                status=fas_status or {},
            )
        except Exception as exc:
            errors["fas"] = str(exc)

    kpis: dict[str, Any] = {
        "cumulative_export_inspections_yoy_pct": (
            fgis_section["summary"]["cumulative_yoy_pct"] if fgis_section else None
        ),
        "current_my_sales_progress_pct": None,
        "next_my_total_sales_mt": (
            fas_section["next_summary"]["world_total_presales_mt"] if fas_section else None
        ),
        "china_next_my_total_purchases_mt": (
            fas_section["next_summary"]["china_total_presales_mt"] if fas_section else None
        ),
    }
    kpi_reasons: dict[str, str | None] = {
        "cumulative_export_inspections_yoy_pct": (
            None if kpis["cumulative_export_inspections_yoy_pct"] is not None else "无同 MY 周桶可比数据"
        ),
        "current_my_sales_progress_pct": None,
        "next_my_total_sales_mt": None if fas_section else errors["fas"],
        "china_next_my_total_purchases_mt": None if fas_section else errors["fas"],
    }
    if fas_section:
        current = fas_section["current_summary"]
        progress, denominator, null_reason, psd_error = _optional_sales_progress(
            Path(usda_project_root),
            target_market_year_end=current["target_market_year_end"],
            commitments_mt=current["world_total_commitments_mt"],
        )
        current["sales_progress_pct"] = progress
        current["sales_progress_denominator"] = denominator
        current["sales_progress_null_reason"] = null_reason
        kpis["current_my_sales_progress_pct"] = progress
        kpi_reasons["current_my_sales_progress_pct"] = null_reason
        errors["psd"] = psd_error
    elif errors["fas"]:
        kpi_reasons["current_my_sales_progress_pct"] = errors["fas"]

    return {
        "schema_version": SCHEMA_VERSION,
        "fgis": fgis_section,
        "fas": fas_section,
        "kpis": kpis,
        "kpi_reasons": kpi_reasons,
        "errors": errors,
    }


def _load_published_source(
    paths: Mapping[str, Path], validator: Any
) -> tuple[pd.DataFrame | None, dict[str, Any], dict[str, Any], str | None]:
    try:
        stable_path = Path(paths["stable"])
        manifest_path = Path(paths["stable_manifest"])
        if not stable_path.is_file() or not manifest_path.is_file():
            raise PipelineError("stable 或 Manifest 不存在")
        manifest = _read_json_object(manifest_path)
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise PipelineError("Manifest schema_version 不受支持")
        if manifest.get("sha256") != sha256_file(stable_path):
            raise PipelineError("stable SHA-256 与 Manifest 不一致")
        frame = pd.read_parquet(stable_path)
        if int(manifest.get("row_count", -1)) != len(frame):
            raise PipelineError("stable 行数与 Manifest 不一致")
        validator(frame)
        status_path = Path(paths["status"])
        status = _read_json_object(status_path) if status_path.is_file() else {}
        return frame, manifest, status, None
    except Exception as exc:
        return None, {}, {}, str(exc)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"无法读取 JSON 身份文件：{path.name}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"JSON 身份文件不是对象：{path.name}")
    return value


def _build_fgis_page_section(
    stable: pd.DataFrame,
    *,
    manifest: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    view = build_fgis_research_view(stable)
    years = sorted(int(value) for value in view["market_year_end"].unique())[-7:]
    history = view.loc[view["market_year_end"].isin(years)].copy()
    latest = history.sort_values(["market_year_end", "my_week"]).iloc[-1]
    current_year = int(latest["market_year_end"])
    world_cumulative_comparison = _same_week_comparison(
        history,
        year_field="market_year_end",
        week_field="my_week",
        value_field="world_cumulative_mt",
        current_year=current_year,
    )
    china_cumulative_comparison = _same_week_comparison(
        history,
        year_field="market_year_end",
        week_field="my_week",
        value_field="china_cumulative_mt",
        current_year=current_year,
    )
    previous_week = history.loc[
        history["market_year_end"].eq(current_year)
        & history["my_week"].eq(int(latest["my_week"]) - 1)
    ]
    previous_cumulative = world_cumulative_comparison["previous_same_week_mt"]
    cumulative_yoy = world_cumulative_comparison["yoy_pct"]
    return {
        "source": "USDA FGIS Grain Inspections",
        "available": True,
        "health": _status_health(status),
        "status": dict(status),
        "manifest": _manifest_identity(manifest),
        "market_years": years,
        "current_market_year_end": current_year,
        "previous_market_year_end": current_year - 1,
        "series": _records(history),
        "seasonal_comparisons": {
            "world_cumulative_mt": world_cumulative_comparison,
            "china_cumulative_mt": china_cumulative_comparison,
        },
        "summary": {
            "market_year_end": current_year,
            "market_year_label": str(latest["market_year_label"]),
            "latest_week": latest["week_ending_date"].date().isoformat(),
            "my_week": int(latest["my_week"]),
            "weekly_world_mt": int(latest["world_weekly_mt"]),
            "previous_week_world_mt": (
                int(previous_week.iloc[0]["world_weekly_mt"])
                if len(previous_week) == 1
                else None
            ),
            "cumulative_world_mt": int(latest["world_cumulative_mt"]),
            "previous_my_same_week_cumulative_mt": previous_cumulative,
            "cumulative_yoy_pct": cumulative_yoy,
            "weekly_china_mt": int(latest["china_weekly_mt"]),
            "weekly_non_china_mt": int(latest["non_china_weekly_mt"]),
            "china_share_pct": _optional_float(latest["china_share_pct"]),
            "previous_week_initial_mt": None,
            "previous_week_initial_observed": False,
        },
    }


def _build_fas_page_section(
    stable: pd.DataFrame,
    *,
    manifest: Mapping[str, Any],
    status: Mapping[str, Any],
) -> dict[str, Any]:
    view = build_fas_research_view(stable)
    years = sorted(int(value) for value in view["report_market_year_end"].unique())[-7:]
    history = view.loc[view["report_market_year_end"].isin(years)].copy()
    latest = history.sort_values(["report_market_year_end", "report_week"]).iloc[-1]
    current_year = int(latest["report_market_year_end"])
    comparison_fields = (
        "world_current_my_total_commitment_mt",
        "china_current_my_total_commitment_mt",
        "world_next_my_outstanding_sales_mt",
        "china_next_my_outstanding_sales_mt",
    )
    return {
        "source": "USDA FAS Export Sales Reporting",
        "available": True,
        "health": _status_health(status),
        "status": dict(status),
        "manifest": _manifest_identity(manifest),
        "report_market_years": years,
        "current_report_market_year_end": current_year,
        "previous_report_market_year_end": current_year - 1,
        "series": _records(history),
        "seasonal_comparisons": {
            field: _same_week_comparison(
                history,
                year_field="report_market_year_end",
                week_field="report_week",
                value_field=field,
                current_year=current_year,
            )
            for field in comparison_fields
        },
        "current_summary": {
            "report_market_year_end": current_year,
            "report_market_year_label": str(latest["report_market_year_label"]),
            "target_market_year_end": int(latest["current_target_market_year_end"]),
            "report_week": int(latest["report_week"]),
            "latest_week": latest["week_ending_date"].date().isoformat(),
            "world_weekly_net_sales_mt": int(latest["world_current_my_net_sales_mt"]),
            "china_weekly_net_sales_mt": int(latest["china_current_my_net_sales_mt"]),
            "non_china_weekly_net_sales_mt": int(latest["non_china_current_my_net_sales_mt"]),
            "world_total_commitments_mt": int(latest["world_current_my_total_commitment_mt"]),
            "china_total_commitments_mt": int(latest["china_current_my_total_commitment_mt"]),
            "non_china_total_commitments_mt": int(latest["non_china_current_my_total_commitment_mt"]),
            "world_accumulated_exports_mt": int(latest["world_accumulated_exports_mt"]),
            "world_outstanding_sales_mt": int(latest["world_outstanding_sales_mt"]),
            "sales_progress_pct": None,
            "sales_progress_denominator": None,
            "sales_progress_null_reason": None,
        },
        "next_summary": {
            "report_market_year_end": current_year,
            "report_market_year_label": str(latest["report_market_year_label"]),
            "target_market_year_end": int(latest["next_target_market_year_end"]),
            "report_week": int(latest["report_week"]),
            "latest_week": latest["week_ending_date"].date().isoformat(),
            "world_weekly_net_sales_mt": int(latest["world_next_my_net_sales_mt"]),
            "china_weekly_net_sales_mt": int(latest["china_next_my_net_sales_mt"]),
            "non_china_weekly_net_sales_mt": int(latest["non_china_next_my_net_sales_mt"]),
            "world_total_presales_mt": int(latest["world_next_my_outstanding_sales_mt"]),
            "china_total_presales_mt": int(latest["china_next_my_outstanding_sales_mt"]),
            "non_china_total_presales_mt": int(latest["non_china_next_my_outstanding_sales_mt"]),
        },
        "release_timestamp_raw": manifest.get("source_release_time_raw"),
        "release_timezone": manifest.get("source_release_timezone"),
    }


def _same_week_comparison(
    frame: pd.DataFrame,
    *,
    year_field: str,
    week_field: str,
    value_field: str,
    current_year: int,
) -> dict[str, Any]:
    """Return an exact-bucket display comparison without nearest-week substitution."""

    current_rows = frame.loc[frame[year_field].eq(current_year)].sort_values(week_field)
    if current_rows.empty:
        raise PipelineError(f"当前年度缺少季节性字段：{value_field}")
    current = current_rows.iloc[-1]
    week = int(current[week_field])
    current_value = int(current[value_field]) if pd.notna(current[value_field]) else None
    previous_rows = frame.loc[
        frame[year_field].eq(current_year - 1) & frame[week_field].eq(week)
    ]
    previous_value = (
        int(previous_rows.iloc[0][value_field])
        if len(previous_rows) == 1 and pd.notna(previous_rows.iloc[0][value_field])
        else None
    )
    same_week_values = frame.loc[
        frame[week_field].eq(week) & frame[value_field].notna(), value_field
    ]
    same_week_rank = (
        int(same_week_values.gt(current_value).sum()) + 1
        if current_value is not None
        else None
    )
    yoy = (
        (current_value - previous_value) / abs(previous_value) * 100
        if current_value is not None and previous_value not in (None, 0)
        else None
    )
    return {
        "comparison_week": week,
        "current_mt": current_value,
        "previous_same_week_mt": previous_value,
        "yoy_pct": yoy,
        "same_week_rank": same_week_rank,
        "same_week_comparable_years": int(len(same_week_values)),
    }


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        converted: dict[str, Any] = {}
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                converted[key] = value.date().isoformat()
            elif pd.isna(value):
                converted[key] = None
            elif hasattr(value, "item"):
                converted[key] = value.item()
            else:
                converted[key] = value
        records.append(converted)
    return records


def _manifest_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: manifest.get(key)
        for key in (
            "schema_version",
            "batch_id",
            "source_latest_week",
            "source_release_time_raw",
            "source_release_timezone",
            "sha256",
            "quality_status",
        )
    }


def _status_health(status: Mapping[str, Any]) -> str:
    if status.get("status") == "failed":
        return "warning" if isinstance(status.get("last_success"), dict) else "unavailable"
    return "success" if status else "warning"

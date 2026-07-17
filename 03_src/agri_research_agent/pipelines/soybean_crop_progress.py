"""USDA NASS soybean PLANTED ingestion, normalization, and audit pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from agri_research_agent.core.paths import PROJECT_ROOT
from agri_research_agent.data_sources.usda_client import (
    NASS_API_ENDPOINT,
    SOYBEAN_PLANTED_YEARS,
    Transport,
    build_soybean_crop_weekly_query_grid,
    build_soybean_progress_query_grid,
    fetch_nass_json,
    redact_secret,
)


MISSING_API_KEY_MESSAGE = (
    "缺少 NASS_API_KEY，未抓取数据，也未生成模拟结果。"
)
PROCESSED_COLUMNS = [
    "system",
    "dataset",
    "commodity",
    "metric",
    "geography_level",
    "region_code",
    "region_name",
    "calendar_year",
    "week_ending",
    "reference_period_desc",
    "value_pct",
    "unit",
    "source_short_desc",
    "source_location_desc",
    "source_load_time",
    "retrieved_at_utc",
    "raw_value",
    "suppression_code",
    "raw_snapshot",
]
STABLE_KEY = [
    "system",
    "commodity",
    "metric",
    "geography_level",
    "region_code",
    "week_ending",
    "unit",
]


class MissingNassApiKeyError(RuntimeError):
    """Raised before any output directory or file is created."""


class PlantedCandidateError(RuntimeError):
    """Raised when the real API fields do not identify one PLANTED series."""

    def __init__(self, message: str, candidate_audit: dict[str, Any]):
        super().__init__(message)
        self.candidate_audit = candidate_audit


def soybean_progress_paths(project_root: Path = PROJECT_ROOT) -> dict[str, Path]:
    """Resolve this module inside the repository's existing numbered layout."""

    return {
        "raw_dir": project_root / "01_data" / "raw" / "soybean_crop_progress",
        "processed": project_root
        / "01_data"
        / "processed"
        / "soybean_crop_progress"
        / "soybeans_planted_weekly_2021_2026.parquet",
        "audit_json": project_root
        / "06_outputs"
        / "audits"
        / "soybean_crop_progress"
        / "soybeans_planted_api_validation.json",
        "audit_markdown": project_root
        / "06_outputs"
        / "audits"
        / "soybean_crop_progress"
        / "soybeans_planted_api_validation.md",
        "log_dir": project_root / "10_logs" / "soybean_crop_progress",
    }


def _relative(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _git_head(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def _stage_path(target: Path, token: str) -> Path:
    suffix = target.suffix
    if suffix:
        return target.parent / f".{target.stem}.{token}.tmp{suffix}"
    return target.parent / f".{target.name}.{token}.tmp"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
        + "\n",
        encoding="utf-8",
    )


def _atomic_replace_many(staged_targets: list[tuple[Path, Path]]) -> None:
    """Replace a group and restore every prior target if any switch fails."""

    token = uuid.uuid4().hex
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        for _, target in staged_targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = target.parent / f".{target.name}.{token}.rollback"
                shutil.copy2(target, backup)
                backups[target] = backup
            else:
                backups[target] = None
        for staged, target in staged_targets:
            os.replace(staged, target)
            replaced.append(target)
    except Exception as exc:
        restore_errors: list[str] = []
        for target in reversed(replaced):
            backup = backups.get(target)
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, target)
            except OSError as restore_exc:
                restore_errors.append(f"{target}: {restore_exc}")
        if restore_errors:
            raise RuntimeError(
                "Output switch failed and rollback was incomplete: "
                + "; ".join(restore_errors)
            ) from exc
        raise
    finally:
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def _json_default(value: object) -> object:
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _candidate_catalog(raw_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: dict[tuple[str, str, str], dict[str, Any]] = {}
    for request in raw_requests:
        for row in request["response"]["data"]:
            key = (
                str(row.get("short_desc", "")).strip(),
                str(row.get("statisticcat_desc", "")).strip(),
                str(row.get("unit_desc", "")).strip(),
            )
            item = catalog.setdefault(
                key,
                {
                    "short_desc": key[0],
                    "statisticcat_desc": key[1],
                    "unit_desc": key[2],
                    "reference_period_desc": set(),
                    "record_count": 0,
                },
            )
            item["record_count"] += 1
            reference = str(row.get("reference_period_desc", "")).strip()
            if reference:
                item["reference_period_desc"].add(reference)
    result = []
    for item in catalog.values():
        result.append(
            {
                **item,
                "reference_period_desc": sorted(item["reference_period_desc"]),
            }
        )
    return sorted(result, key=lambda item: item["short_desc"])


def identify_planted_candidate(
    raw_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select PLANTED only from fields observed in the official response."""

    candidates = _candidate_catalog(raw_requests)
    eligible = [
        item
        for item in candidates
        if item["statisticcat_desc"] == "PROGRESS"
        and item["unit_desc"] == "PCT PLANTED"
        and "PLANTED" in item["short_desc"].upper().split()
    ]
    excluded = []
    for item in candidates:
        if item in eligible:
            continue
        reasons = []
        if item["statisticcat_desc"] != "PROGRESS":
            reasons.append("statisticcat_desc is not PROGRESS")
        if item["unit_desc"] != "PCT PLANTED":
            reasons.append(f"unit_desc is {item['unit_desc'] or '<blank>'}, not PCT PLANTED")
        if "PLANTED" not in item["short_desc"].upper().split():
            reasons.append("short_desc does not identify PLANTED")
        excluded.append({**item, "exclusion_reason": "; ".join(reasons)})

    audit = {
        "all_candidates": candidates,
        "selected_short_desc": eligible[0]["short_desc"] if len(eligible) == 1 else None,
        "selection_basis": (
            "Official rows uniquely match statisticcat_desc=PROGRESS, "
            "unit_desc=PCT PLANTED, and the PLANTED token in short_desc."
        ),
        "excluded_candidates": excluded,
    }
    if len(eligible) != 1:
        descriptions = [item["short_desc"] for item in eligible]
        raise PlantedCandidateError(
            "Expected exactly one unambiguous PLANTED short_desc; "
            f"found {len(eligible)}: {descriptions}",
            audit,
        )
    return audit


def _parse_value(raw: object) -> tuple[float | pd.NA, str | pd.NA]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return pd.NA, pd.NA
    text = str(raw).strip()
    if not text:
        return pd.NA, pd.NA
    try:
        return float(text.replace(",", "")), pd.NA
    except ValueError:
        return pd.NA, text


def normalize_planted_rows(
    raw_requests: list[dict[str, Any]],
    *,
    selected_short_desc: str,
    retrieved_at_utc: str,
    raw_snapshot: str,
) -> tuple[pd.DataFrame, int]:
    """Normalize official PLANTED records without interpolation or aggregation."""

    seen: set[str] = set()
    duplicate_count = 0
    output: list[dict[str, Any]] = []
    for request_index, request in enumerate(raw_requests):
        query = request["query_params"]
        for record_index, row in enumerate(request["response"]["data"]):
            if str(row.get("short_desc", "")).strip() != selected_short_desc:
                continue
            canonical = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if canonical in seen:
                duplicate_count += 1
                continue
            seen.add(canonical)

            level = str(row.get("agg_level_desc") or query["agg_level_desc"]).upper()
            location = str(row.get("location_desc", "")).strip()
            if level == "NATIONAL":
                if location != "US TOTAL":
                    raise ValueError(
                        "NATIONAL PLANTED row is not the official US TOTAL record: "
                        f"{location or '<blank>'}"
                    )
                geography_level = "US"
                region_code = "US"
                region_name = "US TOTAL"
            elif level == "STATE":
                geography_level = "STATE"
                region_code = str(
                    row.get("state_alpha")
                    or row.get("state_fips_code")
                    or row.get("state_ansi")
                    or ""
                ).strip()
                region_name = str(row.get("state_name") or location).strip()
                if not region_code or not region_name:
                    raise ValueError("STATE PLANTED row lacks an official state code or name")
            else:
                raise ValueError(f"Unexpected aggregate level in PLANTED row: {level}")

            raw_value_object = row.get("Value")
            value_pct, suppression_code = _parse_value(raw_value_object)
            raw_value = (
                pd.NA if raw_value_object is None else str(raw_value_object)
            )
            output.append(
                {
                    "system": "USDA_NASS_QUICKSTATS",
                    "dataset": "CROP_PROGRESS",
                    "commodity": "SOYBEANS",
                    "metric": "PLANTED",
                    "geography_level": geography_level,
                    "region_code": region_code,
                    "region_name": region_name,
                    "calendar_year": int(row.get("year") or query["year"]),
                    "week_ending": pd.to_datetime(
                        row.get("week_ending"), errors="coerce"
                    ),
                    "reference_period_desc": str(
                        row.get("reference_period_desc", "")
                    ).strip(),
                    "value_pct": value_pct,
                    "unit": "PCT",
                    "source_short_desc": selected_short_desc,
                    "source_location_desc": location,
                    "source_load_time": str(row.get("load_time", "")).strip(),
                    "retrieved_at_utc": retrieved_at_utc,
                    "raw_value": raw_value,
                    "suppression_code": suppression_code,
                    "raw_snapshot": raw_snapshot,
                    "_raw_record_source": (
                        f"{raw_snapshot}#request={request_index}"
                        f"&record={record_index}"
                    ),
                }
            )

    frame = pd.DataFrame(output)
    if frame.empty:
        raise ValueError("The selected PLANTED short_desc returned no records")
    frame["calendar_year"] = frame["calendar_year"].astype("Int64")
    frame["value_pct"] = pd.to_numeric(frame["value_pct"], errors="coerce").astype(
        "Float64"
    )
    frame = frame.sort_values(
        ["calendar_year", "geography_level", "region_code", "week_ending"],
        na_position="last",
    ).reset_index(drop=True)
    return frame, duplicate_count


def _detail_row(row: pd.Series) -> dict[str, Any]:
    return {
        "geography_level": row["geography_level"],
        "region_code": row["region_code"],
        "region_name": row["region_name"],
        "calendar_year": int(row["calendar_year"]),
        "week_ending": row["week_ending"],
        "value_pct": row["value_pct"],
        "raw_value": row["raw_value"],
        "suppression_code": row["suppression_code"],
        "source_load_time": row["source_load_time"],
        "raw_record_source": row["_raw_record_source"],
    }


def analyze_quality(frame: pd.DataFrame, *, exact_duplicate_count: int) -> dict[str, Any]:
    """Report issues without modifying or filling source values."""

    invalid_dates = frame.loc[frame["week_ending"].isna()]
    range_rows = frame.loc[
        frame["value_pct"].notna()
        & ((frame["value_pct"] < 0) | (frame["value_pct"] > 100))
    ]
    special_rows = frame.loc[
        frame["value_pct"].isna()
        & frame["raw_value"].astype("string").fillna("").str.strip().ne("")
    ]
    missing_rows = frame.loc[
        frame["value_pct"].isna()
        & frame["raw_value"].astype("string").fillna("").str.strip().eq("")
    ]

    duplicate_groups: list[dict[str, Any]] = []
    conflict_groups: list[dict[str, Any]] = []
    for key, group in frame.groupby(STABLE_KEY, dropna=False, sort=False):
        if len(group) <= 1:
            continue
        duplicate_groups.append(
            {
                "stable_key": dict(zip(STABLE_KEY, key, strict=True)),
                "record_count": len(group),
            }
        )
        signatures = {
            (
                None if pd.isna(row["value_pct"]) else float(row["value_pct"]),
                None if pd.isna(row["raw_value"]) else str(row["raw_value"]),
                None
                if pd.isna(row["suppression_code"])
                else str(row["suppression_code"]),
            )
            for _, row in group.iterrows()
        }
        if len(signatures) > 1:
            conflict_groups.append(
                {
                    "stable_key": dict(zip(STABLE_KEY, key, strict=True)),
                    "records": [_detail_row(row) for _, row in group.iterrows()],
                }
            )

    declines: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    group_columns = ["geography_level", "region_code", "region_name", "calendar_year"]
    for group_key, group in frame.groupby(group_columns, sort=False):
        ordered = group.dropna(subset=["week_ending"]).sort_values("week_ending")
        comparable = ordered.dropna(subset=["value_pct"])
        for position in range(1, len(comparable)):
            previous = comparable.iloc[position - 1]
            current = comparable.iloc[position]
            decrease = float(previous["value_pct"] - current["value_pct"])
            if decrease > 0:
                declines.append(
                    {
                        "geography_level": group_key[0],
                        "region_code": group_key[1],
                        "region_name": group_key[2],
                        "calendar_year": int(group_key[3]),
                        "previous_week": previous["week_ending"],
                        "previous_value_pct": previous["value_pct"],
                        "next_week": current["week_ending"],
                        "next_value_pct": current["value_pct"],
                        "decrease_percentage_points": decrease,
                        "previous_raw_record_source": previous["_raw_record_source"],
                        "next_raw_record_source": current["_raw_record_source"],
                    }
                )
        for position in range(1, len(ordered)):
            previous = ordered.iloc[position - 1]
            current = ordered.iloc[position]
            gap_days = int((current["week_ending"] - previous["week_ending"]).days)
            if gap_days > 8:
                gaps.append(
                    {
                        "geography_level": group_key[0],
                        "region_code": group_key[1],
                        "region_name": group_key[2],
                        "calendar_year": int(group_key[3]),
                        "previous_week": previous["week_ending"],
                        "next_week": current["week_ending"],
                        "gap_days": gap_days,
                    }
                )

    return {
        "special_value_count": len(special_rows),
        "missing_value_count": len(missing_rows),
        "complete_duplicate_count": exact_duplicate_count,
        "duplicate_week_count": len(duplicate_groups),
        "stable_key_conflict_count": len(conflict_groups),
        "planting_progress_decline_count": len(declines),
        "date_anomaly_count": len(invalid_dates),
        "range_anomaly_count": len(range_rows),
        "abnormal_gap_count": len(gaps),
        "interpolation_or_fill_applied": False,
        "duplicate_weeks": duplicate_groups,
        "stable_key_conflicts": conflict_groups,
        "planting_progress_declines": declines,
        "date_anomalies": [_detail_row(row) for _, row in invalid_dates.iterrows()],
        "range_anomalies": [_detail_row(row) for _, row in range_rows.iterrows()],
        "special_values": [_detail_row(row) for _, row in special_rows.iterrows()],
        "abnormal_gaps": gaps,
    }


def _yearly_summary(
    frame: pd.DataFrame, raw_requests: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for year in SOYBEAN_PLANTED_YEARS:
        year_rows = frame.loc[frame["calendar_year"].eq(year)]
        national = year_rows.loc[year_rows["geography_level"].eq("US")]
        states = year_rows.loc[year_rows["geography_level"].eq("STATE")]
        national_valid = national.dropna(subset=["week_ending"]).sort_values(
            "week_ending"
        )
        latest = national_valid.iloc[-1] if not national_valid.empty else None
        raw_count = sum(
            request["record_count"]
            for request in raw_requests
            if int(request["query_params"]["year"]) == year
        )
        result[str(year)] = {
            "raw_progress_records": raw_count,
            "processed_planted_records": len(year_rows),
            "national_records": len(national),
            "state_records": len(states),
            "state_coverage_count": int(states["region_code"].nunique()),
            "us_total_exists": bool(
                national["region_name"].eq("US TOTAL").any()
            ),
            "us_total_earliest_week": (
                national_valid["week_ending"].min()
                if not national_valid.empty
                else None
            ),
            "us_total_latest_week": (
                national_valid["week_ending"].max()
                if not national_valid.empty
                else None
            ),
            "us_total_latest_value_pct": (
                latest["value_pct"] if latest is not None else None
            ),
        }
    return result


def _md_table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    def clean(value: object) -> str:
        if value is None or pd.isna(value):
            return ""
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_audit_markdown(audit: dict[str, Any], frame: pd.DataFrame) -> str:
    quality = audit["data_quality"]
    lines = [
        "# USDA NASS 美豆 PLANTED 数据链路审计",
        "",
        f"- API 成功：`{str(audit['api_success']).lower()}`",
        f"- 可进入下一阶段：`{str(audit['ready_for_next_stage']).lower()}`",
        f"- 抓取时间（UTC）：`{audit['retrieved_at_utc']}`",
        "- 全国值来源：USDA 官方 NATIONAL / US TOTAL；未使用州级数据生成或覆盖。",
        "- 单位说明：Quick Stats 原始单位为阶段化的 `PCT PLANTED` 等；"
        "查询使用 `unit_desc__LIKE=PCT` 审计全部百分比候选，标准化单位为 `PCT`。",
        "",
        "## 最终查询条件",
        "",
        _md_table(
            ["参数", "值"],
            audit["actual_query_params_common"].items(),
        ),
        "",
        "## short_desc 候选与选择",
        "",
        f"最终选中：`{audit['candidates']['selected_short_desc']}`",
        "",
        f"选择依据：{audit['candidates']['selection_basis']}",
        "",
        _md_table(
            ["short_desc", "statisticcat_desc", "unit_desc", "记录数", "结论"],
            (
                [
                    item["short_desc"],
                    item["statisticcat_desc"],
                    item["unit_desc"],
                    item["record_count"],
                    (
                        "选中"
                        if item["short_desc"]
                        == audit["candidates"]["selected_short_desc"]
                        else next(
                            (
                                excluded["exclusion_reason"]
                                for excluded in audit["candidates"]["excluded_candidates"]
                                if excluded["short_desc"] == item["short_desc"]
                            ),
                            "排除",
                        )
                    ),
                ]
                for item in audit["candidates"]["all_candidates"]
            ),
        ),
        "",
        "## API 请求清单",
        "",
        _md_table(
            ["年份", "层级", "HTTP", "记录数", "重试", "抓取时间（UTC）"],
            (
                [
                    item["year"],
                    item["agg_level_desc"],
                    item["http_status"],
                    item["record_count"],
                    item["retry_count"],
                    item["fetched_at_utc"],
                ]
                for item in audit["requests"]
            ),
        ),
        "",
        "## 年度覆盖",
        "",
        _md_table(
            [
                "年份",
                "Raw 记录",
                "PLANTED 记录",
                "NATIONAL",
                "STATE",
                "州数",
                "US TOTAL 最早周",
                "US TOTAL 最晚周",
                "最新值(%)",
            ],
            (
                [
                    year,
                    item["raw_progress_records"],
                    item["processed_planted_records"],
                    item["national_records"],
                    item["state_records"],
                    item["state_coverage_count"],
                    item["us_total_earliest_week"],
                    item["us_total_latest_week"],
                    item["us_total_latest_value_pct"],
                ]
                for year, item in audit["yearly_summary"].items()
            ),
        ),
        "",
        "## 数据质量",
        "",
        _md_table(
            ["检查项", "数量"],
            [
                ["特殊值", quality["special_value_count"]],
                ["空值", quality["missing_value_count"]],
                ["完全重复", quality["complete_duplicate_count"]],
                ["重复周次", quality["duplicate_week_count"]],
                ["稳定键冲突", quality["stable_key_conflict_count"]],
                ["种植进度下降", quality["planting_progress_decline_count"]],
                ["日期异常", quality["date_anomaly_count"]],
                ["0–100 范围异常", quality["range_anomaly_count"]],
                ["异常断档", quality["abnormal_gap_count"]],
            ],
        ),
        "",
        "## 2026 未完成年度",
        "",
        f"- 已识别为未完成年度：`{str(audit['incomplete_2026']['is_incomplete']).lower()}`",
        "- 尚未发布的未来周次不计为错误，也没有插值、前后填充或补零。",
        "",
        "## 文件与哈希",
        "",
        _md_table(
            ["文件", "路径", "SHA-256", "记录数"],
            [
                [
                    "Raw",
                    audit["files"]["raw_snapshot"]["path"],
                    audit["files"]["raw_snapshot"]["sha256"],
                    audit["files"]["raw_snapshot"]["record_count"],
                ],
                [
                    "Processed",
                    audit["files"]["processed"]["path"],
                    audit["files"]["processed"]["sha256"],
                    audit["files"]["processed"]["record_count"],
                ],
            ],
        ),
    ]

    us_total = frame.loc[
        frame["geography_level"].eq("US") & frame["region_name"].eq("US TOTAL")
    ].sort_values(["calendar_year", "week_ending"])
    lines.extend(["", "## 各年度 US TOTAL 数据预览"])
    for year in SOYBEAN_PLANTED_YEARS:
        year_rows = us_total.loc[us_total["calendar_year"].eq(year)]
        preview = pd.concat([year_rows.head(3), year_rows.tail(3)]).drop_duplicates()
        lines.extend(
            [
                "",
                f"### {year}",
                "",
                _md_table(
                    ["week_ending", "reference_period_desc", "value_pct", "raw_value"],
                    (
                        [
                            row["week_ending"],
                            row["reference_period_desc"],
                            row["value_pct"],
                            row["raw_value"],
                        ]
                        for _, row in preview.iterrows()
                    ),
                ),
                "",
                (
                    "最新数据："
                    + (
                        f"`{year_rows.iloc[-1]['week_ending']:%Y-%m-%d}` = "
                        f"`{year_rows.iloc[-1]['value_pct']}`%"
                        if not year_rows.empty
                        else "无"
                    )
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def _build_manifest(
    *,
    raw_requests: list[dict[str, Any]],
    raw_path: str,
    raw_sha256: str,
    git_head: str,
    retrieved_at_utc: str,
    overall_success: bool,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "api_endpoint": NASS_API_ENDPOINT,
        "years": list(SOYBEAN_PLANTED_YEARS),
        "aggregate_levels": ["NATIONAL", "STATE"],
        "retrieved_at_utc": retrieved_at_utc,
        "requests": [
            {
                "year": int(item["query_params"]["year"]),
                "agg_level_desc": item["query_params"]["agg_level_desc"],
                "query_params": item["query_params"],
                "record_count": item["record_count"],
                "http_status": item["http_status"],
                "fetched_at_utc": item["fetched_at_utc"],
                "retry_count": item["retry_count"],
                "response_sha256": item["response_sha256"],
            }
            for item in raw_requests
        ],
        "raw_snapshot": raw_path,
        "raw_snapshot_sha256": raw_sha256,
        "git_head": git_head,
        "overall_success": overall_success,
        "error": error,
    }


def _failure_markdown(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates", {}).get("all_candidates", [])
    return "\n".join(
        [
            "# USDA NASS 美豆 PLANTED 数据链路审计（失败）",
            "",
            f"错误：{payload['error']}",
            "",
            "未覆盖已有 Processed 文件。",
            "",
            "## short_desc 候选",
            "",
            _md_table(
                ["short_desc", "statisticcat_desc", "unit_desc", "记录数"],
                (
                    [
                        item["short_desc"],
                        item["statisticcat_desc"],
                        item["unit_desc"],
                        item["record_count"],
                    ]
                    for item in candidates
                ),
            ),
            "",
        ]
    )


def _make_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"soybean_crop_progress.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def run_soybean_planted_pipeline(
    *,
    project_root: Path = PROJECT_ROOT,
    api_key: str | None = None,
    timeout: int = 60,
    retries: int = 2,
    transport: Transport | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Fetch, validate, normalize, audit, and atomically publish PLANTED data."""

    secret = api_key if api_key is not None else os.environ.get("NASS_API_KEY", "")
    if not secret:
        raise MissingNassApiKeyError(MISSING_API_KEY_MESSAGE)

    run_time = now or datetime.now(timezone.utc)
    if run_time.tzinfo is None:
        run_time = run_time.replace(tzinfo=timezone.utc)
    run_time = run_time.astimezone(timezone.utc)
    retrieved_at_utc = run_time.isoformat()
    timestamp = run_time.strftime("%Y%m%dT%H%M%SZ")
    paths = soybean_progress_paths(project_root)
    raw_target = paths["raw_dir"] / (
        f"nass_soybeans_planted_2021_2026_{timestamp}.json"
    )
    manifest_target = paths["raw_dir"] / (
        f"nass_soybeans_planted_2021_2026_{timestamp}_manifest.json"
    )
    if raw_target.exists() or manifest_target.exists():
        raise FileExistsError("Refusing to overwrite an existing immutable Raw snapshot")
    logger = _make_logger(
        paths["log_dir"] / f"nass_soybeans_planted_{run_time:%Y%m%d}.log"
    )
    token = uuid.uuid4().hex
    staged_paths: list[Path] = []

    try:
        raw_requests: list[dict[str, Any]] = []
        for query in build_soybean_progress_query_grid():
            result = fetch_nass_json(
                query,
                api_key=secret,
                timeout=timeout,
                retries=retries,
                transport=transport,
                sleep=sleep or __import__("time").sleep,
            )
            item = {
                "query_params": query,
                "http_status": result["http_status"],
                "record_count": result["record_count"],
                "retry_count": result["retry_count"],
                "fetched_at_utc": result["fetched_at_utc"],
                "response_sha256": _payload_sha256(result["payload"]),
                "response": result["payload"],
            }
            raw_requests.append(item)
            logger.info(
                "Fetched year=%s level=%s status=%s records=%s retries=%s",
                query["year"],
                query["agg_level_desc"],
                item["http_status"],
                item["record_count"],
                item["retry_count"],
            )

        raw_payload = {
            "schema_version": 1,
            "api_endpoint": NASS_API_ENDPOINT,
            "retrieved_at_utc": retrieved_at_utc,
            "requests": [
                {
                    "query_params": item["query_params"],
                    "http_status": item["http_status"],
                    "fetched_at_utc": item["fetched_at_utc"],
                    "response_sha256": item["response_sha256"],
                    "response": item["response"],
                }
                for item in raw_requests
            ],
        }
        raw_stage = _stage_path(raw_target, token)
        staged_paths.append(raw_stage)
        _write_json(raw_stage, raw_payload)
        with raw_stage.open("r", encoding="utf-8") as file:
            reloaded_raw = json.load(file)
        reloaded_count = sum(
            len(item["response"]["data"])
            for item in reloaded_raw["requests"]
        )
        expected_raw_count = sum(item["record_count"] for item in raw_requests)
        if reloaded_count != expected_raw_count:
            raise ValueError("Raw snapshot verification record count mismatch")
        raw_sha256 = _sha256(raw_stage)
        raw_relative = _relative(raw_target, project_root)

        candidate_audit: dict[str, Any] = {}
        try:
            candidate_audit = identify_planted_candidate(raw_requests)
            selected = str(candidate_audit["selected_short_desc"])
            frame, exact_duplicate_count = normalize_planted_rows(
                raw_requests,
                selected_short_desc=selected,
                retrieved_at_utc=retrieved_at_utc,
                raw_snapshot=raw_relative,
            )
            quality = analyze_quality(
                frame, exact_duplicate_count=exact_duplicate_count
            )
            yearly = _yearly_summary(frame, raw_requests)
            current_2026 = yearly["2026"]
            latest_2026 = current_2026["us_total_latest_week"]
            incomplete_2026 = {
                "is_incomplete": bool(
                    run_time.year == 2026
                    and latest_2026 is not None
                    and pd.Timestamp(latest_2026).date() < run_time.date()
                ),
                "latest_published_week": latest_2026,
                "future_weeks_missing_is_error": False,
                "future_weeks_filled": False,
                "reason": (
                    "2026 is the current reporting year; unpublished future weeks are "
                    "normal missing observations."
                ),
            }

            readiness_reasons = []
            if any(not item["us_total_exists"] for item in yearly.values()):
                readiness_reasons.append("One or more years lack official US TOTAL records")
            if quality["stable_key_conflict_count"]:
                readiness_reasons.append("Stable-key value conflicts require review")
            if quality["date_anomaly_count"]:
                readiness_reasons.append("Unparseable week-ending dates require review")
            if quality["range_anomaly_count"]:
                readiness_reasons.append("Values outside 0-100 require review")

            processed_stage = _stage_path(paths["processed"], token)
            staged_paths.append(processed_stage)
            processed_stage.parent.mkdir(parents=True, exist_ok=True)
            frame[PROCESSED_COLUMNS].to_parquet(processed_stage, index=False)
            reloaded_processed = pd.read_parquet(processed_stage)
            if len(reloaded_processed) != len(frame):
                raise ValueError("Processed Parquet verification record count mismatch")
            if list(reloaded_processed.columns) != PROCESSED_COLUMNS:
                raise ValueError("Processed Parquet verification column mismatch")
            processed_sha256 = _sha256(processed_stage)

            manifest = _build_manifest(
                raw_requests=raw_requests,
                raw_path=raw_relative,
                raw_sha256=raw_sha256,
                git_head=_git_head(project_root),
                retrieved_at_utc=retrieved_at_utc,
                overall_success=True,
            )
            request_audit = manifest["requests"]
            audit = {
                "schema_version": 1,
                "api_success": True,
                "processing_success": True,
                "retrieved_at_utc": retrieved_at_utc,
                "api_endpoint": NASS_API_ENDPOINT,
                "intended_query_contract": {
                    "source_desc": "SURVEY",
                    "sector_desc": "CROPS",
                    "group_desc": "FIELD CROPS",
                    "commodity_desc": "SOYBEANS",
                    "freq_desc": "WEEKLY",
                    "statisticcat_desc": "PROGRESS",
                    "unit_desc": "PCT",
                    "years": list(SOYBEAN_PLANTED_YEARS),
                    "aggregate_levels": ["NATIONAL", "STATE"],
                },
                "actual_query_params_common": {
                    key: value
                    for key, value in raw_requests[0]["query_params"].items()
                    if key not in {"year", "agg_level_desc"}
                },
                "unit_query_note": (
                    "Quick Stats rejects unit_desc=PCT because official crop-progress "
                    "units are stage-specific. unit_desc__LIKE=PCT retrieves the "
                    "auditable candidate set, including PCT PLANTED."
                ),
                "requests": request_audit,
                "candidates": candidate_audit,
                "files": {
                    "raw_snapshot": {
                        "path": raw_relative,
                        "sha256": raw_sha256,
                        "record_count": expected_raw_count,
                    },
                    "manifest": {"path": _relative(manifest_target, project_root)},
                    "processed": {
                        "path": _relative(paths["processed"], project_root),
                        "sha256": processed_sha256,
                        "record_count": len(frame),
                    },
                },
                "raw_total_records": expected_raw_count,
                "processed_total_records": len(frame),
                "yearly_summary": yearly,
                "data_quality": quality,
                "incomplete_2026": incomplete_2026,
                "state_data_used_for_us_total": False,
                "us_total_provenance": (
                    "Only official rows returned by agg_level_desc=NATIONAL with "
                    "location_desc=US TOTAL are normalized as geography_level=US."
                ),
                "ready_for_next_stage": not readiness_reasons,
                "readiness_reasons": readiness_reasons,
            }
            markdown = render_audit_markdown(audit, frame)

            manifest_stage = _stage_path(manifest_target, token)
            audit_json_stage = _stage_path(paths["audit_json"], token)
            audit_md_stage = _stage_path(paths["audit_markdown"], token)
            staged_paths.extend([manifest_stage, audit_json_stage, audit_md_stage])
            _write_json(manifest_stage, manifest)
            _write_json(audit_json_stage, audit)
            audit_md_stage.parent.mkdir(parents=True, exist_ok=True)
            audit_md_stage.write_text(markdown, encoding="utf-8")
            json.loads(manifest_stage.read_text(encoding="utf-8"))
            json.loads(audit_json_stage.read_text(encoding="utf-8"))
            if not audit_md_stage.read_text(encoding="utf-8").strip():
                raise ValueError("Markdown audit verification failed")

            _atomic_replace_many(
                [
                    (raw_stage, raw_target),
                    (manifest_stage, manifest_target),
                    (processed_stage, paths["processed"]),
                    (audit_json_stage, paths["audit_json"]),
                    (audit_md_stage, paths["audit_markdown"]),
                ]
            )
            logger.info(
                "Published raw_records=%s processed_records=%s ready=%s",
                expected_raw_count,
                len(frame),
                audit["ready_for_next_stage"],
            )
            return audit
        except Exception as exc:
            safe_error = redact_secret(
                f"{type(exc).__name__}: {exc}", secret
            )
            if isinstance(exc, PlantedCandidateError):
                candidate_audit = exc.candidate_audit
            manifest = _build_manifest(
                raw_requests=raw_requests,
                raw_path=raw_relative,
                raw_sha256=raw_sha256,
                git_head=_git_head(project_root),
                retrieved_at_utc=retrieved_at_utc,
                overall_success=False,
                error=safe_error,
            )
            failure_audit = {
                "schema_version": 1,
                "api_success": True,
                "processing_success": False,
                "retrieved_at_utc": retrieved_at_utc,
                "error": safe_error,
                "candidates": candidate_audit,
                "raw_snapshot": {
                    "path": raw_relative,
                    "sha256": raw_sha256,
                    "record_count": expected_raw_count,
                },
                "processed_file_preserved": True,
                "ready_for_next_stage": False,
            }
            manifest_stage = _stage_path(manifest_target, token)
            audit_json_stage = _stage_path(paths["audit_json"], token)
            audit_md_stage = _stage_path(paths["audit_markdown"], token)
            staged_paths.extend([manifest_stage, audit_json_stage, audit_md_stage])
            _write_json(manifest_stage, manifest)
            _write_json(audit_json_stage, failure_audit)
            audit_md_stage.parent.mkdir(parents=True, exist_ok=True)
            audit_md_stage.write_text(
                _failure_markdown(failure_audit), encoding="utf-8"
            )
            _atomic_replace_many(
                [
                    (raw_stage, raw_target),
                    (manifest_stage, manifest_target),
                    (audit_json_stage, paths["audit_json"]),
                    (audit_md_stage, paths["audit_markdown"]),
                ]
            )
            logger.error("Processing failed safely: %s", safe_error)
            raise
    finally:
        for path in staged_paths:
            path.unlink(missing_ok=True)
        _close_logger(logger)


# Unified second-stage crop progress and condition pipeline.
PROGRESS_UNIT_TO_METRIC = {
    "PCT PLANTED": "PLANTED",
    "PCT EMERGED": "EMERGED",
    "PCT BLOOMING": "BLOOMING",
    "PCT SETTING PODS": "SETTING_PODS",
    "PCT FULLY PODDED": "FULLY_PODDED",
    "PCT COLORING": "COLORING",
    "PCT DROPPING LEAVES": "DROPPING_LEAVES",
    "PCT MATURE": "MATURE",
    "PCT HARVESTED": "HARVESTED",
}
CONDITION_UNIT_TO_METRIC = {
    "PCT VERY POOR": "VERY_POOR",
    "PCT POOR": "POOR",
    "PCT FAIR": "FAIR",
    "PCT GOOD": "GOOD",
    "PCT EXCELLENT": "EXCELLENT",
}
CONDITION_DIRECT_METRICS = tuple(CONDITION_UNIT_TO_METRIC.values())
CROP_WEEKLY_COLUMNS = [
    "system",
    "dataset",
    "metric_family",
    "commodity",
    "metric",
    "geography_level",
    "region_code",
    "region_name",
    "calendar_year",
    "week_ending",
    "reference_period_desc",
    "value_pct",
    "unit",
    "source_statisticcat_desc",
    "source_unit_desc",
    "source_short_desc",
    "source_location_desc",
    "source_load_time",
    "retrieved_at_utc",
    "raw_value",
    "suppression_code",
    "raw_snapshot",
    "is_derived",
    "derivation_formula",
    "derivation_good_value_pct",
    "derivation_excellent_value_pct",
    "derivation_source_records",
]
CROP_STABLE_KEY = [
    "system",
    "commodity",
    "metric_family",
    "metric",
    "geography_level",
    "region_code",
    "week_ending",
    "unit",
]
PLANTED_COMPATIBILITY_FIELDS = [
    "system",
    "dataset",
    "commodity",
    "metric",
    "geography_level",
    "region_code",
    "region_name",
    "calendar_year",
    "week_ending",
    "reference_period_desc",
    "value_pct",
    "unit",
    "source_short_desc",
    "source_location_desc",
    "source_load_time",
    "raw_value",
    "suppression_code",
]


class CropMetricMappingError(RuntimeError):
    """Raised when official crop-weekly fields cannot be mapped uniquely."""

    def __init__(self, message: str, candidate_audit: dict[str, Any]):
        super().__init__(message)
        self.candidate_audit = candidate_audit


def soybean_crop_weekly_paths(project_root: Path = PROJECT_ROOT) -> dict[str, Path]:
    base = project_root / "01_data" / "processed" / "soybean_crop_progress"
    audit = project_root / "06_outputs" / "audits" / "soybean_crop_progress"
    return {
        "raw_dir": project_root / "01_data" / "raw" / "soybean_crop_progress",
        "progress": base / "soybeans_crop_progress_weekly_2021_2026.parquet",
        "condition": base / "soybeans_crop_condition_weekly_2021_2026.parquet",
        "planted_baseline": base / "soybeans_planted_weekly_2021_2026.parquet",
        "audit_json": audit / "soybeans_crop_weekly_validation.json",
        "audit_markdown": audit / "soybeans_crop_weekly_validation.md",
        "log_dir": project_root / "10_logs" / "soybean_crop_progress",
    }


def identify_crop_metric_mappings(
    raw_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Audit every candidate and require exact three-field metric mappings."""

    candidates = _candidate_catalog(raw_requests)
    expected = {
        "PROGRESS": PROGRESS_UNIT_TO_METRIC,
        "CONDITION": CONDITION_UNIT_TO_METRIC,
    }
    final_mapping: list[dict[str, Any]] = []
    errors: list[str] = []

    for family, unit_mapping in expected.items():
        for unit_desc, metric in unit_mapping.items():
            expected_short_desc = (
                f"SOYBEANS - {family}, MEASURED IN {unit_desc}"
            )
            matches = [
                item
                for item in candidates
                if item["statisticcat_desc"] == family
                and item["unit_desc"] == unit_desc
                and item["short_desc"] == expected_short_desc
            ]
            if len(matches) != 1:
                errors.append(
                    f"{family}/{metric} expected one exact candidate; found {len(matches)}"
                )
                continue
            final_mapping.append(
                {
                    "metric_family": family,
                    "metric": metric,
                    "statisticcat_desc": family,
                    "unit_desc": unit_desc,
                    "short_desc": expected_short_desc,
                    "record_count": matches[0]["record_count"],
                }
            )

    expected_triples = {
        (item["statisticcat_desc"], item["unit_desc"], item["short_desc"])
        for item in final_mapping
    }
    unexpected = [
        item
        for item in candidates
        if (
            item["statisticcat_desc"],
            item["unit_desc"],
            item["short_desc"],
        )
        not in expected_triples
    ]
    if unexpected:
        errors.append(
            "Official response contains unmapped candidates: "
            + ", ".join(item["short_desc"] for item in unexpected)
        )
    audit = {
        "all_candidates": candidates,
        "final_metric_mapping": sorted(
            final_mapping, key=lambda item: (item["metric_family"], item["metric"])
        ),
        "unexpected_candidates": unexpected,
        "mapping_basis": (
            "Each official metric uniquely matches statisticcat_desc, unit_desc, "
            "and the complete short_desc; no fuzzy keyword-only mapping is used."
        ),
        "mapping_complete": not errors,
        "mapping_errors": errors,
    }
    if errors:
        raise CropMetricMappingError("; ".join(errors), audit)
    return audit


def _metric_lookup(mapping_audit: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    return {
        (
            item["statisticcat_desc"],
            item["unit_desc"],
            item["short_desc"],
        ): item["metric"]
        for item in mapping_audit["final_metric_mapping"]
    }


def _official_geography(
    row: dict[str, Any], query: dict[str, str]
) -> tuple[str, str, str]:
    level = str(row.get("agg_level_desc") or query["agg_level_desc"]).upper()
    location = str(row.get("location_desc", "")).strip()
    if level == "NATIONAL":
        if location != "US TOTAL":
            raise ValueError(
                "NATIONAL crop-weekly row is not official US TOTAL: "
                f"{location or '<blank>'}"
            )
        return "US", "US", "US TOTAL"
    if level == "STATE":
        region_code = str(
            row.get("state_alpha")
            or row.get("state_fips_code")
            or row.get("state_ansi")
            or ""
        ).strip()
        region_name = str(row.get("state_name") or location).strip()
        if not region_code or not region_name:
            raise ValueError("STATE crop-weekly row lacks official code or name")
        return "STATE", region_code, region_name
    raise ValueError(f"Unexpected crop-weekly aggregate level: {level}")


def normalize_crop_weekly_rows(
    raw_requests: list[dict[str, Any]],
    *,
    mapping_audit: dict[str, Any],
    retrieved_at_utc: str,
    raw_snapshot: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Normalize all mapped official metrics without filling missing weeks."""

    lookup = _metric_lookup(mapping_audit)
    seen: dict[str, set[str]] = {"PROGRESS": set(), "CONDITION": set()}
    duplicate_counts = {"PROGRESS": 0, "CONDITION": 0}
    output: dict[str, list[dict[str, Any]]] = {"PROGRESS": [], "CONDITION": []}

    for request_index, request in enumerate(raw_requests):
        query = request["query_params"]
        for record_index, row in enumerate(request["response"]["data"]):
            family = str(row.get("statisticcat_desc", "")).strip()
            triple = (
                family,
                str(row.get("unit_desc", "")).strip(),
                str(row.get("short_desc", "")).strip(),
            )
            metric = lookup.get(triple)
            if metric is None:
                continue
            canonical = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if canonical in seen[family]:
                duplicate_counts[family] += 1
                continue
            seen[family].add(canonical)
            geography_level, region_code, region_name = _official_geography(row, query)
            raw_value_object = row.get("Value")
            value_pct, suppression_code = _parse_value(raw_value_object)
            output[family].append(
                {
                    "system": "USDA_NASS_QUICKSTATS",
                    "dataset": "CROP_PROGRESS",
                    "metric_family": family,
                    "commodity": "SOYBEANS",
                    "metric": metric,
                    "geography_level": geography_level,
                    "region_code": region_code,
                    "region_name": region_name,
                    "calendar_year": int(row.get("year") or query["year"]),
                    "week_ending": pd.to_datetime(
                        row.get("week_ending"), errors="coerce"
                    ),
                    "reference_period_desc": str(
                        row.get("reference_period_desc", "")
                    ).strip(),
                    "value_pct": value_pct,
                    "unit": "PCT",
                    "source_statisticcat_desc": family,
                    "source_unit_desc": triple[1],
                    "source_short_desc": triple[2],
                    "source_location_desc": str(
                        row.get("location_desc", "")
                    ).strip(),
                    "source_load_time": str(row.get("load_time", "")).strip(),
                    "retrieved_at_utc": retrieved_at_utc,
                    "raw_value": (
                        pd.NA if raw_value_object is None else str(raw_value_object)
                    ),
                    "suppression_code": suppression_code,
                    "raw_snapshot": raw_snapshot,
                    "is_derived": False,
                    "derivation_formula": pd.NA,
                    "derivation_good_value_pct": pd.NA,
                    "derivation_excellent_value_pct": pd.NA,
                    "derivation_source_records": pd.NA,
                    "_raw_record_source": (
                        f"{raw_snapshot}#request={request_index}&record={record_index}"
                    ),
                }
            )

    frames: dict[str, pd.DataFrame] = {}
    for family in ("PROGRESS", "CONDITION"):
        frame = pd.DataFrame(output[family])
        if frame.empty:
            raise ValueError(f"Mapped {family} data contains no records")
        frame["calendar_year"] = frame["calendar_year"].astype("Int64")
        frame["value_pct"] = pd.to_numeric(
            frame["value_pct"], errors="coerce"
        ).astype("Float64")
        frame["is_derived"] = frame["is_derived"].astype(bool)
        frame = frame.sort_values(
            ["metric", "calendar_year", "geography_level", "region_code", "week_ending"],
            na_position="last",
        ).reset_index(drop=True)
        frames[family] = frame
    return frames["PROGRESS"], frames["CONDITION"], duplicate_counts


def derive_good_excellent(
    direct_condition: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Derive GOOD_EXCELLENT only from coexisting numeric GOOD and EXCELLENT."""

    base_key = [
        "system",
        "commodity",
        "metric_family",
        "geography_level",
        "region_code",
        "calendar_year",
        "week_ending",
        "unit",
    ]
    derived: list[dict[str, Any]] = []
    skipped_missing = 0
    skipped_ambiguous = 0
    for _, group in direct_condition.groupby(base_key, dropna=False, sort=False):
        good = group.loc[group["metric"].eq("GOOD")]
        excellent = group.loc[group["metric"].eq("EXCELLENT")]
        if len(good) != 1 or len(excellent) != 1:
            skipped_missing += 1
            continue
        good_row = good.iloc[0]
        excellent_row = excellent.iloc[0]
        if pd.isna(good_row["value_pct"]) or pd.isna(excellent_row["value_pct"]):
            skipped_missing += 1
            continue
        if (
            good_row["region_name"] != excellent_row["region_name"]
            or good_row["source_location_desc"]
            != excellent_row["source_location_desc"]
        ):
            skipped_ambiguous += 1
            continue
        good_value = float(good_row["value_pct"])
        excellent_value = float(excellent_row["value_pct"])
        row = good_row.to_dict()
        row.update(
            {
                "metric": "GOOD_EXCELLENT",
                "value_pct": good_value + excellent_value,
                "source_unit_desc": "PCT GOOD + PCT EXCELLENT",
                "source_short_desc": "DERIVED: GOOD + EXCELLENT",
                "source_load_time": max(
                    str(good_row["source_load_time"]),
                    str(excellent_row["source_load_time"]),
                ),
                "raw_value": pd.NA,
                "suppression_code": pd.NA,
                "is_derived": True,
                "derivation_formula": "GOOD + EXCELLENT",
                "derivation_good_value_pct": good_value,
                "derivation_excellent_value_pct": excellent_value,
                "derivation_source_records": json.dumps(
                    {
                        "GOOD": good_row["_raw_record_source"],
                        "EXCELLENT": excellent_row["_raw_record_source"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "_raw_record_source": (
                    "derived(GOOD="
                    f"{good_row['_raw_record_source']},EXCELLENT="
                    f"{excellent_row['_raw_record_source']})"
                ),
            }
        )
        derived.append(row)

    derived_frame = pd.DataFrame(derived, columns=direct_condition.columns)
    combined = pd.concat([direct_condition, derived_frame], ignore_index=True)
    combined["value_pct"] = pd.to_numeric(
        combined["value_pct"], errors="coerce"
    ).astype("Float64")
    combined["derivation_good_value_pct"] = pd.to_numeric(
        combined["derivation_good_value_pct"], errors="coerce"
    ).astype("Float64")
    combined["derivation_excellent_value_pct"] = pd.to_numeric(
        combined["derivation_excellent_value_pct"], errors="coerce"
    ).astype("Float64")
    combined["is_derived"] = combined["is_derived"].astype(bool)
    combined = combined.sort_values(
        ["metric", "calendar_year", "geography_level", "region_code", "week_ending"],
        na_position="last",
    ).reset_index(drop=True)
    return combined, {
        "formula": "GOOD + EXCELLENT",
        "derived_count": len(derived_frame),
        "skipped_missing_component_count": skipped_missing,
        "skipped_ambiguous_component_count": skipped_ambiguous,
        "missing_components_treated_as_zero": False,
    }


def _crop_detail_row(row: pd.Series) -> dict[str, Any]:
    return {
        "metric_family": row["metric_family"],
        "metric": row["metric"],
        "geography_level": row["geography_level"],
        "region_code": row["region_code"],
        "region_name": row["region_name"],
        "calendar_year": int(row["calendar_year"]),
        "week_ending": row["week_ending"],
        "value_pct": row["value_pct"],
        "raw_value": row["raw_value"],
        "suppression_code": row["suppression_code"],
        "source_load_time": row["source_load_time"],
        "raw_record_source": row["_raw_record_source"],
    }


def analyze_crop_weekly_quality(
    progress: pd.DataFrame,
    condition: pd.DataFrame,
    *,
    exact_duplicate_counts: dict[str, int],
) -> dict[str, Any]:
    """Apply family-appropriate checks without changing source values."""

    combined = pd.concat([progress, condition], ignore_index=True)
    invalid_dates = combined.loc[combined["week_ending"].isna()]
    range_rows = combined.loc[
        combined["value_pct"].notna()
        & ((combined["value_pct"] < 0) | (combined["value_pct"] > 100))
    ]
    special_rows = combined.loc[
        ~combined["is_derived"]
        & combined["value_pct"].isna()
        & combined["raw_value"].astype("string").fillna("").str.strip().ne("")
    ]
    missing_rows = combined.loc[
        ~combined["is_derived"]
        & combined["value_pct"].isna()
        & combined["raw_value"].astype("string").fillna("").str.strip().eq("")
    ]

    duplicate_groups: list[dict[str, Any]] = []
    conflict_groups: list[dict[str, Any]] = []
    for key, group in combined.groupby(CROP_STABLE_KEY, dropna=False, sort=False):
        if len(group) <= 1:
            continue
        duplicate_groups.append(
            {
                "stable_key": dict(zip(CROP_STABLE_KEY, key, strict=True)),
                "record_count": len(group),
            }
        )
        signatures = {
            (
                None if pd.isna(row["value_pct"]) else float(row["value_pct"]),
                None if pd.isna(row["raw_value"]) else str(row["raw_value"]),
                None
                if pd.isna(row["suppression_code"])
                else str(row["suppression_code"]),
            )
            for _, row in group.iterrows()
        }
        if len(signatures) > 1:
            conflict_groups.append(
                {
                    "stable_key": dict(zip(CROP_STABLE_KEY, key, strict=True)),
                    "records": [_crop_detail_row(row) for _, row in group.iterrows()],
                }
            )

    progress_declines: list[dict[str, Any]] = []
    progress_group = [
        "metric",
        "geography_level",
        "region_code",
        "region_name",
        "calendar_year",
    ]
    for group_key, group in progress.groupby(progress_group, sort=False):
        comparable = group.dropna(
            subset=["week_ending", "value_pct"]
        ).sort_values("week_ending")
        for position in range(1, len(comparable)):
            previous = comparable.iloc[position - 1]
            current = comparable.iloc[position]
            decrease = float(previous["value_pct"] - current["value_pct"])
            if decrease > 0:
                progress_declines.append(
                    {
                        "metric": group_key[0],
                        "geography_level": group_key[1],
                        "region_code": group_key[2],
                        "region_name": group_key[3],
                        "calendar_year": int(group_key[4]),
                        "previous_week": previous["week_ending"],
                        "previous_value_pct": previous["value_pct"],
                        "next_week": current["week_ending"],
                        "next_value_pct": current["value_pct"],
                        "decrease_percentage_points": decrease,
                        "previous_raw_record_source": previous["_raw_record_source"],
                        "next_raw_record_source": current["_raw_record_source"],
                    }
                )

    direct_condition = condition.loc[~condition["is_derived"]]
    condition_base_key = [
        "geography_level",
        "region_code",
        "region_name",
        "calendar_year",
        "week_ending",
    ]
    condition_sum_checked = 0
    condition_sum_incomplete = 0
    condition_sum_anomalies: list[dict[str, Any]] = []
    required = set(CONDITION_DIRECT_METRICS)
    for key, group in direct_condition.groupby(
        condition_base_key, dropna=False, sort=False
    ):
        values: dict[str, float] = {}
        complete = True
        for metric in required:
            rows = group.loc[group["metric"].eq(metric)]
            if len(rows) != 1 or pd.isna(rows.iloc[0]["value_pct"]):
                complete = False
                break
            values[metric] = float(rows.iloc[0]["value_pct"])
        if not complete:
            condition_sum_incomplete += 1
            continue
        condition_sum_checked += 1
        total = sum(values.values())
        if total < 99 or total > 101:
            condition_sum_anomalies.append(
                {
                    "geography_level": key[0],
                    "region_code": key[1],
                    "region_name": key[2],
                    "calendar_year": int(key[3]),
                    "week_ending": key[4],
                    "component_values": values,
                    "condition_sum_pct": total,
                }
            )

    derived_rows = condition.loc[condition["metric"].eq("GOOD_EXCELLENT")]
    derived_mismatches: list[dict[str, Any]] = []
    for _, row in derived_rows.iterrows():
        expected = (
            float(row["derivation_good_value_pct"])
            + float(row["derivation_excellent_value_pct"])
        )
        if pd.isna(row["value_pct"]) or abs(float(row["value_pct"]) - expected) > 1e-9:
            derived_mismatches.append(_crop_detail_row(row))

    national_source_anomalies = combined.loc[
        combined["geography_level"].eq("US")
        & ~combined["source_location_desc"].eq("US TOTAL")
    ]
    return {
        "special_value_count": len(special_rows),
        "missing_value_count": len(missing_rows),
        "complete_duplicate_count": sum(exact_duplicate_counts.values()),
        "complete_duplicate_count_by_family": exact_duplicate_counts,
        "duplicate_stable_key_count": len(duplicate_groups),
        "stable_key_conflict_count": len(conflict_groups),
        "progress_decline_count": len(progress_declines),
        "condition_sum_checked_count": condition_sum_checked,
        "condition_sum_incomplete_count": condition_sum_incomplete,
        "condition_sum_anomaly_count": len(condition_sum_anomalies),
        "good_excellent_mismatch_count": len(derived_mismatches),
        "date_anomaly_count": len(invalid_dates),
        "range_anomaly_count": len(range_rows),
        "national_source_anomaly_count": len(national_source_anomalies),
        "progress_monotonicity_check_applied": True,
        "condition_monotonicity_check_applied": False,
        "interpolation_or_fill_applied": False,
        "duplicate_stable_keys": duplicate_groups,
        "stable_key_conflicts": conflict_groups,
        "progress_declines": progress_declines,
        "condition_sum_anomalies": condition_sum_anomalies,
        "good_excellent_mismatches": derived_mismatches,
        "date_anomalies": [_crop_detail_row(row) for _, row in invalid_dates.iterrows()],
        "range_anomalies": [_crop_detail_row(row) for _, row in range_rows.iterrows()],
        "special_values": [_crop_detail_row(row) for _, row in special_rows.iterrows()],
        "national_source_anomalies": [
            _crop_detail_row(row) for _, row in national_source_anomalies.iterrows()
        ],
    }


def summarize_crop_metrics(
    progress: pd.DataFrame, condition: pd.DataFrame
) -> dict[str, dict[str, Any]]:
    combined = pd.concat([progress, condition], ignore_index=True)
    result: dict[str, dict[str, Any]] = {}
    for (family, metric), rows in combined.groupby(
        ["metric_family", "metric"], sort=True
    ):
        national = rows.loc[rows["geography_level"].eq("US")]
        states = rows.loc[rows["geography_level"].eq("STATE")]
        national_valid = national.dropna(subset=["week_ending"]).sort_values(
            "week_ending"
        )
        latest = national_valid.iloc[-1] if not national_valid.empty else None
        coverage_years = sorted(int(value) for value in rows["calendar_year"].unique())
        state_coverage_by_year = {
            str(year): int(
                states.loc[states["calendar_year"].eq(year), "region_code"].nunique()
            )
            for year in coverage_years
        }
        national_presence = {
            str(year): bool(national["calendar_year"].eq(year).any())
            for year in SOYBEAN_PLANTED_YEARS
        }
        result[f"{family}.{metric}"] = {
            "metric_family": family,
            "metric": metric,
            "record_count": len(rows),
            "national_record_count": len(national),
            "state_record_count": len(states),
            "coverage_years": coverage_years,
            "state_coverage_count": int(states["region_code"].nunique()),
            "state_coverage_by_year": state_coverage_by_year,
            "national_presence_by_year": national_presence,
            "missing_national_years": [
                year for year, exists in national_presence.items() if not exists
            ],
            "us_total_earliest_week": (
                national_valid["week_ending"].min()
                if not national_valid.empty
                else None
            ),
            "us_total_latest_week": (
                national_valid["week_ending"].max()
                if not national_valid.empty
                else None
            ),
            "us_total_latest_value_pct": (
                latest["value_pct"] if latest is not None else None
            ),
        }
    return result


def _canonical_business_records(
    frame: pd.DataFrame, fields: list[str]
) -> Counter[str]:
    records: list[str] = []
    for _, row in frame[fields].iterrows():
        payload: dict[str, Any] = {}
        for field in fields:
            value = row[field]
            if pd.isna(value):
                payload[field] = None
            elif field == "week_ending":
                payload[field] = pd.Timestamp(value).strftime("%Y-%m-%d")
            elif field == "calendar_year":
                payload[field] = int(value)
            elif field == "value_pct":
                payload[field] = float(value)
            else:
                payload[field] = str(value)
        records.append(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    return Counter(records)


def compare_planted_with_first_step(
    progress: pd.DataFrame, baseline_path: Path
) -> dict[str, Any]:
    if not baseline_path.is_file():
        raise FileNotFoundError(
            f"First-step PLANTED baseline is missing: {baseline_path}"
        )
    baseline = pd.read_parquet(baseline_path)
    planted = progress.loc[progress["metric"].eq("PLANTED")]
    missing_old = [field for field in PLANTED_COMPATIBILITY_FIELDS if field not in baseline]
    missing_new = [field for field in PLANTED_COMPATIBILITY_FIELDS if field not in planted]
    if missing_old or missing_new:
        raise ValueError(
            f"PLANTED compatibility fields missing; old={missing_old}, new={missing_new}"
        )
    old_records = _canonical_business_records(
        baseline, PLANTED_COMPATIBILITY_FIELDS
    )
    new_records = _canonical_business_records(
        planted, PLANTED_COMPATIBILITY_FIELDS
    )
    old_only = old_records - new_records
    new_only = new_records - old_records
    result = {
        "business_fields": PLANTED_COMPATIBILITY_FIELDS,
        "first_step_record_count": len(baseline),
        "unified_progress_planted_record_count": len(planted),
        "old_only_record_count": sum(old_only.values()),
        "new_only_record_count": sum(new_only.values()),
        "business_fields_exact_match": not old_only and not new_only,
        "first_step_file": str(baseline_path),
        "first_step_file_sha256": _sha256(baseline_path),
    }
    if not result["business_fields_exact_match"]:
        raise ValueError(
            "Unified PLANTED business fields differ from first-step baseline: "
            f"old_only={result['old_only_record_count']}, "
            f"new_only={result['new_only_record_count']}"
        )
    return result


def _build_crop_weekly_manifest(
    *,
    raw_requests: list[dict[str, Any]],
    raw_path: str,
    raw_sha256: str,
    git_head: str,
    retrieved_at_utc: str,
    overall_success: bool,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "api_endpoint": NASS_API_ENDPOINT,
        "years": list(SOYBEAN_PLANTED_YEARS),
        "statistic_categories": ["PROGRESS", "CONDITION"],
        "aggregate_levels": ["NATIONAL", "STATE"],
        "retrieved_at_utc": retrieved_at_utc,
        "requests": [
            {
                "year": int(item["query_params"]["year"]),
                "statisticcat_desc": item["query_params"]["statisticcat_desc"],
                "agg_level_desc": item["query_params"]["agg_level_desc"],
                "query_params": item["query_params"],
                "record_count": item["record_count"],
                "http_status": item["http_status"],
                "fetched_at_utc": item["fetched_at_utc"],
                "retry_count": item["retry_count"],
                "response_sha256": item["response_sha256"],
            }
            for item in raw_requests
        ],
        "raw_record_counts": {
            category: sum(
                item["record_count"]
                for item in raw_requests
                if item["query_params"]["statisticcat_desc"] == category
            )
            for category in ("PROGRESS", "CONDITION")
        },
        "raw_snapshot": raw_path,
        "raw_snapshot_sha256": raw_sha256,
        "git_head": git_head,
        "overall_success": overall_success,
        "error": error,
    }


def render_crop_weekly_markdown(
    audit: dict[str, Any], progress: pd.DataFrame, condition: pd.DataFrame
) -> str:
    quality = audit["data_quality"]
    lines = [
        "# USDA NASS 美豆生长阶段与作物状况数据审计",
        "",
        f"- API 成功：`{str(audit['api_success']).lower()}`",
        f"- 处理成功：`{str(audit['processing_success']).lower()}`",
        f"- 可进入下一阶段：`{str(audit['ready_for_next_stage']).lower()}`",
        f"- 抓取时间（UTC）：`{audit['retrieved_at_utc']}`",
        "- 全国数据仅来自 USDA NATIONAL / US TOTAL；州级数据未生成或覆盖全国值。",
        "- 未执行插值、补零、前向填充或后向填充。",
        "",
        "## 最终指标映射",
        "",
        _md_table(
            ["指标族", "source unit_desc", "source short_desc", "metric", "Raw 记录"],
            (
                [
                    item["metric_family"],
                    item["unit_desc"],
                    item["short_desc"],
                    item["metric"],
                    item["record_count"],
                ]
                for item in audit["candidates"]["final_metric_mapping"]
            ),
        ),
        "",
        "## API 请求",
        "",
        _md_table(
            ["年份", "类别", "层级", "HTTP", "记录数", "重试"],
            (
                [
                    item["year"],
                    item["statisticcat_desc"],
                    item["agg_level_desc"],
                    item["http_status"],
                    item["record_count"],
                    item["retry_count"],
                ]
                for item in audit["requests"]
            ),
        ),
        "",
        "## 指标覆盖摘要",
        "",
        _md_table(
            [
                "指标族",
                "指标",
                "记录数",
                "NATIONAL",
                "STATE",
                "年份",
                "州数",
                "US 最早周",
                "US 最晚周",
                "最新 US 值",
            ],
            (
                [
                    item["metric_family"],
                    item["metric"],
                    item["record_count"],
                    item["national_record_count"],
                    item["state_record_count"],
                    ", ".join(str(year) for year in item["coverage_years"]),
                    item["state_coverage_count"],
                    item["us_total_earliest_week"],
                    item["us_total_latest_week"],
                    item["us_total_latest_value_pct"],
                ]
                for item in audit["metric_summaries"].values()
            ),
        ),
        "",
        "## GOOD_EXCELLENT 派生",
        "",
        f"- 公式：`{audit['good_excellent']['formula']}`",
        f"- 派生记录数：`{audit['good_excellent']['derived_count']}`",
        f"- 缺少组成值而未派生：`{audit['good_excellent']['skipped_missing_component_count']}`",
        f"- 派生等式异常：`{quality['good_excellent_mismatch_count']}`",
        "",
        "## 质量检查",
        "",
        _md_table(
            ["检查", "数量"],
            [
                ["五档状况合计检查", quality["condition_sum_checked_count"]],
                ["五档状况合计不完整", quality["condition_sum_incomplete_count"]],
                ["五档状况合计超出 99–101", quality["condition_sum_anomaly_count"]],
                ["生长进度下降", quality["progress_decline_count"]],
                ["完全重复", quality["complete_duplicate_count"]],
                ["重复稳定键", quality["duplicate_stable_key_count"]],
                ["不同值冲突", quality["stable_key_conflict_count"]],
                ["日期异常", quality["date_anomaly_count"]],
                ["0–100 范围异常", quality["range_anomaly_count"]],
                ["特殊值", quality["special_value_count"]],
                ["全国来源异常", quality["national_source_anomaly_count"]],
            ],
        ),
        "",
        "作物状况未执行单调性检查；只有 PROGRESS 按地区、年度、具体 metric 检查累计值下降。",
        "",
        "## PLANTED 第一步一致性",
        "",
        f"- 第一步记录数：`{audit['planted_compatibility']['first_step_record_count']}`",
        f"- 统一文件 PLANTED 记录数：`{audit['planted_compatibility']['unified_progress_planted_record_count']}`",
        f"- 业务字段完全一致：`{str(audit['planted_compatibility']['business_fields_exact_match']).lower()}`",
        "",
        "## 文件与 SHA-256",
        "",
        _md_table(
            ["文件", "路径", "记录数", "SHA-256"],
            [
                [
                    "Raw",
                    audit["files"]["raw_snapshot"]["path"],
                    audit["files"]["raw_snapshot"]["record_count"],
                    audit["files"]["raw_snapshot"]["sha256"],
                ],
                [
                    "Progress",
                    audit["files"]["progress"]["path"],
                    audit["files"]["progress"]["record_count"],
                    audit["files"]["progress"]["sha256"],
                ],
                [
                    "Condition",
                    audit["files"]["condition"]["path"],
                    audit["files"]["condition"]["record_count"],
                    audit["files"]["condition"]["sha256"],
                ],
            ],
        ),
        "",
        "## 各指标最新年度 US TOTAL 预览",
    ]
    combined = pd.concat([progress, condition], ignore_index=True)
    for key, summary in audit["metric_summaries"].items():
        family, metric = key.split(".", 1)
        latest_year = max(summary["coverage_years"])
        rows = combined.loc[
            combined["metric_family"].eq(family)
            & combined["metric"].eq(metric)
            & combined["geography_level"].eq("US")
            & combined["calendar_year"].eq(latest_year)
        ].sort_values("week_ending")
        preview = pd.concat([rows.head(3), rows.tail(3)]).drop_duplicates()
        lines.extend(
            [
                "",
                f"### {family}.{metric}（{latest_year}）",
                "",
                _md_table(
                    ["week_ending", "reference_period_desc", "value_pct", "is_derived"],
                    (
                        [
                            row["week_ending"],
                            row["reference_period_desc"],
                            row["value_pct"],
                            row["is_derived"],
                        ]
                        for _, row in preview.iterrows()
                    ),
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def _crop_failure_markdown(payload: dict[str, Any]) -> str:
    mappings = payload.get("candidates", {}).get("all_candidates", [])
    return "\n".join(
        [
            "# USDA NASS 美豆周度统一链路审计（失败）",
            "",
            f"错误：{payload['error']}",
            "",
            "已有 Progress、Condition 和第一步 PLANTED 文件均未覆盖。",
            "",
            "## 官方候选",
            "",
            _md_table(
                ["statisticcat_desc", "unit_desc", "short_desc", "记录数"],
                (
                    [
                        item["statisticcat_desc"],
                        item["unit_desc"],
                        item["short_desc"],
                        item["record_count"],
                    ]
                    for item in mappings
                ),
            ),
            "",
        ]
    )


def run_soybean_crop_weekly_pipeline(
    *,
    project_root: Path = PROJECT_ROOT,
    api_key: str | None = None,
    timeout: int = 60,
    retries: int = 2,
    transport: Transport | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Publish unified official PROGRESS, CONDITION, and derived condition data."""

    secret = api_key if api_key is not None else os.environ.get("NASS_API_KEY", "")
    if not secret:
        raise MissingNassApiKeyError(MISSING_API_KEY_MESSAGE)
    run_time = now or datetime.now(timezone.utc)
    if run_time.tzinfo is None:
        run_time = run_time.replace(tzinfo=timezone.utc)
    run_time = run_time.astimezone(timezone.utc)
    retrieved_at_utc = run_time.isoformat()
    timestamp = run_time.strftime("%Y%m%dT%H%M%SZ")
    paths = soybean_crop_weekly_paths(project_root)
    raw_target = paths["raw_dir"] / (
        f"nass_soybeans_crop_weekly_2021_2026_{timestamp}.json"
    )
    manifest_target = paths["raw_dir"] / (
        f"nass_soybeans_crop_weekly_2021_2026_{timestamp}_manifest.json"
    )
    if raw_target.exists() or manifest_target.exists():
        raise FileExistsError("Refusing to overwrite an existing crop-weekly Raw snapshot")
    logger = _make_logger(
        paths["log_dir"] / f"nass_soybeans_crop_weekly_{run_time:%Y%m%d}.log"
    )
    token = uuid.uuid4().hex
    staged_paths: list[Path] = []

    try:
        raw_requests: list[dict[str, Any]] = []
        for query in build_soybean_crop_weekly_query_grid():
            result = fetch_nass_json(
                query,
                api_key=secret,
                timeout=timeout,
                retries=retries,
                transport=transport,
                sleep=sleep or __import__("time").sleep,
            )
            item = {
                "query_params": query,
                "http_status": result["http_status"],
                "record_count": result["record_count"],
                "retry_count": result["retry_count"],
                "fetched_at_utc": result["fetched_at_utc"],
                "response_sha256": _payload_sha256(result["payload"]),
                "response": result["payload"],
            }
            raw_requests.append(item)
            logger.info(
                "Fetched year=%s category=%s level=%s status=%s records=%s retries=%s",
                query["year"],
                query["statisticcat_desc"],
                query["agg_level_desc"],
                item["http_status"],
                item["record_count"],
                item["retry_count"],
            )

        raw_payload = {
            "schema_version": 2,
            "api_endpoint": NASS_API_ENDPOINT,
            "retrieved_at_utc": retrieved_at_utc,
            "requests": [
                {
                    "query_params": item["query_params"],
                    "http_status": item["http_status"],
                    "fetched_at_utc": item["fetched_at_utc"],
                    "response_sha256": item["response_sha256"],
                    "response": item["response"],
                }
                for item in raw_requests
            ],
        }
        expected_raw_count = sum(item["record_count"] for item in raw_requests)
        raw_stage = _stage_path(raw_target, token)
        staged_paths.append(raw_stage)
        _write_json(raw_stage, raw_payload)
        reloaded_raw = json.loads(raw_stage.read_text(encoding="utf-8"))
        reloaded_raw_count = sum(
            len(item["response"]["data"])
            for item in reloaded_raw["requests"]
        )
        if reloaded_raw_count != expected_raw_count:
            raise ValueError("Unified Raw verification record count mismatch")
        raw_sha256 = _sha256(raw_stage)
        raw_relative = _relative(raw_target, project_root)
        mapping_audit: dict[str, Any] = {}

        try:
            mapping_audit = identify_crop_metric_mappings(raw_requests)
            progress, direct_condition, duplicate_counts = normalize_crop_weekly_rows(
                raw_requests,
                mapping_audit=mapping_audit,
                retrieved_at_utc=retrieved_at_utc,
                raw_snapshot=raw_relative,
            )
            condition, derivation_audit = derive_good_excellent(direct_condition)
            planted_compatibility = compare_planted_with_first_step(
                progress, paths["planted_baseline"]
            )
            quality = analyze_crop_weekly_quality(
                progress,
                condition,
                exact_duplicate_counts=duplicate_counts,
            )
            metric_summaries = summarize_crop_metrics(progress, condition)
            all_rows = pd.concat([progress, condition], ignore_index=True)
            latest_2026 = all_rows.loc[
                all_rows["calendar_year"].eq(2026), "week_ending"
            ].max()
            incomplete_2026 = {
                "is_incomplete": run_time.year == 2026,
                "latest_published_week_across_metrics": latest_2026,
                "future_weeks_missing_is_error": False,
                "future_weeks_filled": False,
                "different_metric_week_ranges_forced_equal": False,
                "reason": (
                    "2026 is the current reporting year. Unpublished future weeks and "
                    "different official start/end weeks by metric are preserved as-is."
                ),
            }

            readiness_reasons: list[str] = []
            blocking_quality = {
                "stable-key conflicts": quality["stable_key_conflict_count"],
                "date anomalies": quality["date_anomaly_count"],
                "range anomalies": quality["range_anomaly_count"],
                "condition-sum anomalies": quality["condition_sum_anomaly_count"],
                "GOOD_EXCELLENT mismatches": quality["good_excellent_mismatch_count"],
                "national-source anomalies": quality["national_source_anomaly_count"],
            }
            for label, count in blocking_quality.items():
                if count:
                    readiness_reasons.append(f"{label}: {count}")
            if not planted_compatibility["business_fields_exact_match"]:
                readiness_reasons.append("PLANTED differs from first-step baseline")

            progress_stage = _stage_path(paths["progress"], token)
            condition_stage = _stage_path(paths["condition"], token)
            staged_paths.extend([progress_stage, condition_stage])
            progress_stage.parent.mkdir(parents=True, exist_ok=True)
            progress[CROP_WEEKLY_COLUMNS].to_parquet(progress_stage, index=False)
            condition[CROP_WEEKLY_COLUMNS].to_parquet(condition_stage, index=False)
            reloaded_progress = pd.read_parquet(progress_stage)
            reloaded_condition = pd.read_parquet(condition_stage)
            if len(reloaded_progress) != len(progress):
                raise ValueError("Progress Parquet verification record count mismatch")
            if len(reloaded_condition) != len(condition):
                raise ValueError("Condition Parquet verification record count mismatch")
            if list(reloaded_progress.columns) != CROP_WEEKLY_COLUMNS:
                raise ValueError("Progress Parquet verification column mismatch")
            if list(reloaded_condition.columns) != CROP_WEEKLY_COLUMNS:
                raise ValueError("Condition Parquet verification column mismatch")
            progress_sha256 = _sha256(progress_stage)
            condition_sha256 = _sha256(condition_stage)

            manifest = _build_crop_weekly_manifest(
                raw_requests=raw_requests,
                raw_path=raw_relative,
                raw_sha256=raw_sha256,
                git_head=_git_head(project_root),
                retrieved_at_utc=retrieved_at_utc,
                overall_success=True,
            )
            raw_counts = manifest["raw_record_counts"]
            audit = {
                "schema_version": 2,
                "api_success": True,
                "processing_success": True,
                "retrieved_at_utc": retrieved_at_utc,
                "api_endpoint": NASS_API_ENDPOINT,
                "requests": manifest["requests"],
                "query_contracts": {
                    "PROGRESS": {
                        "statisticcat_desc": "PROGRESS",
                        "unit_desc__LIKE": "PCT",
                    },
                    "CONDITION": {
                        "statisticcat_desc": "CONDITION",
                        "unit_desc__LIKE": "PCT",
                    },
                    "split_by": ["year", "statisticcat_desc", "agg_level_desc"],
                },
                "candidates": mapping_audit,
                "raw_record_counts": raw_counts,
                "standardized_record_counts": {
                    "PROGRESS": len(progress),
                    "CONDITION_OFFICIAL": len(direct_condition),
                    "CONDITION_WITH_DERIVED": len(condition),
                },
                "metric_summaries": metric_summaries,
                "good_excellent": derivation_audit,
                "data_quality": quality,
                "planted_compatibility": planted_compatibility,
                "incomplete_2026": incomplete_2026,
                "state_data_used_for_us_total": False,
                "us_total_provenance": (
                    "Official metrics use only agg_level_desc=NATIONAL rows with "
                    "location_desc=US TOTAL. Derived US GOOD_EXCELLENT uses only the "
                    "official US TOTAL GOOD and EXCELLENT components for the same week."
                ),
                "files": {
                    "raw_snapshot": {
                        "path": raw_relative,
                        "sha256": raw_sha256,
                        "record_count": expected_raw_count,
                    },
                    "manifest": {"path": _relative(manifest_target, project_root)},
                    "progress": {
                        "path": _relative(paths["progress"], project_root),
                        "sha256": progress_sha256,
                        "record_count": len(progress),
                    },
                    "condition": {
                        "path": _relative(paths["condition"], project_root),
                        "sha256": condition_sha256,
                        "record_count": len(condition),
                    },
                },
                "ready_for_next_stage": not readiness_reasons,
                "readiness_reasons": readiness_reasons,
            }
            markdown = render_crop_weekly_markdown(audit, progress, condition)

            manifest_stage = _stage_path(manifest_target, token)
            audit_json_stage = _stage_path(paths["audit_json"], token)
            audit_md_stage = _stage_path(paths["audit_markdown"], token)
            staged_paths.extend([manifest_stage, audit_json_stage, audit_md_stage])
            _write_json(manifest_stage, manifest)
            _write_json(audit_json_stage, audit)
            audit_md_stage.parent.mkdir(parents=True, exist_ok=True)
            audit_md_stage.write_text(markdown, encoding="utf-8")
            json.loads(manifest_stage.read_text(encoding="utf-8"))
            json.loads(audit_json_stage.read_text(encoding="utf-8"))
            if not audit_md_stage.read_text(encoding="utf-8").strip():
                raise ValueError("Unified Markdown audit verification failed")

            _atomic_replace_many(
                [
                    (raw_stage, raw_target),
                    (manifest_stage, manifest_target),
                    (progress_stage, paths["progress"]),
                    (condition_stage, paths["condition"]),
                    (audit_json_stage, paths["audit_json"]),
                    (audit_md_stage, paths["audit_markdown"]),
                ]
            )
            logger.info(
                "Published raw=%s progress=%s condition=%s derived=%s ready=%s",
                expected_raw_count,
                len(progress),
                len(condition),
                derivation_audit["derived_count"],
                audit["ready_for_next_stage"],
            )
            return audit
        except Exception as exc:
            safe_error = redact_secret(f"{type(exc).__name__}: {exc}", secret)
            if isinstance(exc, CropMetricMappingError):
                mapping_audit = exc.candidate_audit
            manifest = _build_crop_weekly_manifest(
                raw_requests=raw_requests,
                raw_path=raw_relative,
                raw_sha256=raw_sha256,
                git_head=_git_head(project_root),
                retrieved_at_utc=retrieved_at_utc,
                overall_success=False,
                error=safe_error,
            )
            failure_audit = {
                "schema_version": 2,
                "api_success": True,
                "processing_success": False,
                "retrieved_at_utc": retrieved_at_utc,
                "error": safe_error,
                "candidates": mapping_audit,
                "raw_snapshot": {
                    "path": raw_relative,
                    "sha256": raw_sha256,
                    "record_count": expected_raw_count,
                },
                "progress_file_preserved": True,
                "condition_file_preserved": True,
                "planted_baseline_preserved": True,
                "ready_for_next_stage": False,
            }
            manifest_stage = _stage_path(manifest_target, token)
            audit_json_stage = _stage_path(paths["audit_json"], token)
            audit_md_stage = _stage_path(paths["audit_markdown"], token)
            staged_paths.extend([manifest_stage, audit_json_stage, audit_md_stage])
            _write_json(manifest_stage, manifest)
            _write_json(audit_json_stage, failure_audit)
            audit_md_stage.parent.mkdir(parents=True, exist_ok=True)
            audit_md_stage.write_text(
                _crop_failure_markdown(failure_audit), encoding="utf-8"
            )
            _atomic_replace_many(
                [
                    (raw_stage, raw_target),
                    (manifest_stage, manifest_target),
                    (audit_json_stage, paths["audit_json"]),
                    (audit_md_stage, paths["audit_markdown"]),
                ]
            )
            logger.error("Unified processing failed safely: %s", safe_error)
            raise
    finally:
        for path in staged_paths:
            path.unlink(missing_ok=True)
        _close_logger(logger)

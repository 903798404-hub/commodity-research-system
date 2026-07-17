"""Safe current-year updater for USDA soybean crop progress and condition."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from agri_research_agent.core.paths import PROJECT_ROOT
from agri_research_agent.data_sources.usda_client import (
    NASS_API_ENDPOINT,
    Transport,
    build_soybean_crop_weekly_current_year_query_grid,
    fetch_nass_json,
    redact_secret,
)
from agri_research_agent.pipelines.soybean_crop_comparison import (
    load_display_config,
    metric_definitions,
)
from agri_research_agent.pipelines.soybean_crop_progress import (
    CROP_STABLE_KEY,
    CROP_WEEKLY_COLUMNS,
    _make_logger,
    _payload_sha256,
    _sha256,
    analyze_crop_weekly_quality,
    derive_good_excellent,
    identify_crop_metric_mappings,
    normalize_crop_weekly_rows,
)


NEW_YORK = ZoneInfo("America/New_York")
SEASON_START = (4, 1)
SEASON_END = (11, 30)
PROVENANCE_COLUMNS = {
    "retrieved_at_utc",
    "raw_snapshot",
}
BUSINESS_COLUMNS = [
    column for column in CROP_WEEKLY_COLUMNS if column not in PROVENANCE_COLUMNS
]
BLOCKING_QUALITY_COUNTS = (
    "duplicate_stable_key_count",
    "stable_key_conflict_count",
    "progress_decline_count",
    "condition_sum_anomaly_count",
    "good_excellent_mismatch_count",
    "date_anomaly_count",
    "range_anomaly_count",
    "national_source_anomaly_count",
)
GIT_HEAD_ENVIRONMENT_VARIABLE = "MARKET_DATA_GIT_HEAD"
GIT_HEAD_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
GIT_HEAD_FAILURE_MESSAGE = "无法确定部署Git提交，未调用USDA API。"


class SoybeanWeeklyUpdateError(RuntimeError):
    """A safely reported current-year update failure."""


def soybean_weekly_update_paths(
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Path]:
    """Return stable, fallback, audit, status, backup, and log locations."""

    processed = project_root / "01_data" / "processed" / "soybean_crop_progress"
    return {
        "stable_progress": processed / "soybeans_crop_progress_weekly.parquet",
        "stable_condition": processed / "soybeans_crop_condition_weekly.parquet",
        "legacy_progress": (
            processed / "soybeans_crop_progress_weekly_2021_2026.parquet"
        ),
        "legacy_condition": (
            processed / "soybeans_crop_condition_weekly_2021_2026.parquet"
        ),
        "raw_dir": project_root / "01_data" / "raw" / "soybean_crop_progress",
        "candidate_root": (
            project_root / "01_data" / "candidates" / "soybean_crop_progress"
        ),
        "backup_root": (
            project_root / "01_data" / "backups" / "soybean_crop_progress"
        ),
        "status": (
            project_root
            / "01_data"
            / "update_status"
            / "soybean_crop_progress.json"
        ),
        "audit_root": (
            project_root / "06_outputs" / "audits" / "soybean_crop_progress"
        ),
        "log_dir": project_root / "10_logs" / "soybean_crop_progress",
        "display_config": (
            project_root / "02_configs" / "soybean_crop_progress_display.yaml"
        ),
    }


def new_york_reporting_date(now: datetime | None = None) -> date:
    """Return the reporting date used for season and current-year decisions."""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(NEW_YORK).date()


def is_soybean_reporting_season(reporting_date: date) -> bool:
    """Return whether the New York date falls in April 1 through November 30."""

    month_day = (reporting_date.month, reporting_date.day)
    return SEASON_START <= month_day <= SEASON_END


def _utc_now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _relative(path: Path | None, project_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _validate_git_head(value: object, *, source: str) -> str:
    candidate = str(value).strip()
    if not GIT_HEAD_PATTERN.fullmatch(candidate):
        raise SoybeanWeeklyUpdateError(
            f"{GIT_HEAD_FAILURE_MESSAGE}"
            f"{source}必须是40位十六进制Git提交哈希。"
        )
    return candidate.lower()


def resolve_deployment_git_head(
    project_root: Path,
    *,
    explicit_git_head: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve one validated deployment commit before any USDA request."""

    environment = os.environ if env is None else env
    if explicit_git_head is not None:
        return _validate_git_head(
            explicit_git_head,
            source="命令行参数 --git-head ",
        )

    if GIT_HEAD_ENVIRONMENT_VARIABLE in environment:
        return _validate_git_head(
            environment[GIT_HEAD_ENVIRONMENT_VARIABLE],
            source=f"环境变量 {GIT_HEAD_ENVIRONMENT_VARIABLE} ",
        )

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SoybeanWeeklyUpdateError(GIT_HEAD_FAILURE_MESSAGE) from None
    if result.returncode != 0:
        raise SoybeanWeeklyUpdateError(GIT_HEAD_FAILURE_MESSAGE)
    try:
        return _validate_git_head(
            result.stdout,
            source="git rev-parse HEAD 的输出",
        )
    except SoybeanWeeklyUpdateError:
        raise SoybeanWeeklyUpdateError(GIT_HEAD_FAILURE_MESSAGE) from None


def _json_default(value: object) -> object:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is pd.NA or pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_text(payload: object) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        + "\n"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _write_immutable_json_pair(
    first_path: Path,
    first_payload: object,
    second_path: Path,
    second_payload: object,
    *,
    secret: str,
) -> tuple[str, str]:
    """Create a Raw/Manifest pair without ever overwriting an earlier run."""

    if first_path.exists() or second_path.exists():
        raise FileExistsError("Refusing to overwrite an existing Raw or Manifest")
    serialized = (_json_text(first_payload), _json_text(second_payload))
    for text in serialized:
        if secret and secret in text:
            raise ValueError("Secret redaction check failed")
    first_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.parent.mkdir(parents=True, exist_ok=True)
    first_stage = first_path.parent / f".{first_path.name}.{uuid.uuid4().hex}.tmp"
    second_stage = second_path.parent / f".{second_path.name}.{uuid.uuid4().hex}.tmp"
    installed: list[Path] = []
    try:
        first_stage.write_text(serialized[0], encoding="utf-8")
        second_stage.write_text(serialized[1], encoding="utf-8")
        json.loads(first_stage.read_text(encoding="utf-8"))
        json.loads(second_stage.read_text(encoding="utf-8"))
        os.replace(first_stage, first_path)
        installed.append(first_path)
        os.replace(second_stage, second_path)
        installed.append(second_path)
    except Exception:
        for path in installed:
            path.unlink(missing_ok=True)
        raise
    finally:
        first_stage.unlink(missing_ok=True)
        second_stage.unlink(missing_ok=True)
    return _sha256(first_path), _sha256(second_path)


def _scalar(value: object) -> object:
    if value is pd.NA or pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        value = value.item()
    return value


def _record_counter(frame: pd.DataFrame, columns: list[str]) -> Counter[str]:
    records: list[str] = []
    for row in frame[columns].to_dict("records"):
        records.append(
            json.dumps(
                {column: _scalar(row[column]) for column in columns},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return Counter(records)


def _keyed_records(
    frame: pd.DataFrame, columns: list[str]
) -> dict[tuple[object, ...], str]:
    if frame.duplicated(CROP_STABLE_KEY).any():
        raise ValueError("Stable key is not unique while calculating business changes")
    result: dict[tuple[object, ...], str] = {}
    for _, row in frame.iterrows():
        key = tuple(_scalar(row[column]) for column in CROP_STABLE_KEY)
        values = {column: _scalar(row[column]) for column in columns}
        # The existing derivation audit stores Raw row paths in this field.
        # Preserve whether traceability exists, but do not turn a new immutable
        # Raw filename into a business correction.
        if values.get("derivation_source_records") is not None:
            values["derivation_source_records"] = "<TRACEABLE_COMPONENTS>"
        result[key] = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return result


def compare_business_records(
    old: pd.DataFrame, new: pd.DataFrame
) -> dict[str, int | bool]:
    """Count stable-key additions, corrections, and deletions."""

    old_records = _keyed_records(old, BUSINESS_COLUMNS)
    new_records = _keyed_records(new, BUSINESS_COLUMNS)
    old_keys = set(old_records)
    new_keys = set(new_records)
    added = len(new_keys - old_keys)
    deleted = len(old_keys - new_keys)
    corrected = sum(
        old_records[key] != new_records[key] for key in old_keys & new_keys
    )
    return {
        "added": added,
        "corrected": corrected,
        "deleted": deleted,
        "business_changed": bool(added or corrected or deleted),
    }


def _read_baseline_pair(
    paths: Mapping[str, Path],
) -> tuple[pd.DataFrame, pd.DataFrame, bool, str]:
    stable_exists = (
        paths["stable_progress"].is_file(),
        paths["stable_condition"].is_file(),
    )
    if stable_exists[0] != stable_exists[1]:
        raise FileNotFoundError(
            "Stable Progress and Condition must either both exist or both be absent"
        )
    initialized = not all(stable_exists)
    if initialized:
        progress_path = paths["legacy_progress"]
        condition_path = paths["legacy_condition"]
        source = "legacy_2021_2026"
    else:
        progress_path = paths["stable_progress"]
        condition_path = paths["stable_condition"]
        source = "stable"
    if not progress_path.is_file() or not condition_path.is_file():
        raise FileNotFoundError(
            "The complete soybean Progress/Condition baseline pair is unavailable"
        )
    progress = pd.read_parquet(progress_path)
    condition = pd.read_parquet(condition_path)
    for family, frame in (("Progress", progress), ("Condition", condition)):
        if list(frame.columns) != CROP_WEEKLY_COLUMNS:
            raise ValueError(f"{family} baseline columns are incomplete or reordered")
    return progress, condition, initialized, source


def _with_internal_source(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    result = frame.copy()
    result["_raw_record_source"] = [
        f"{label}#row={index}" for index in range(len(result))
    ]
    return result


def merge_current_year(
    baseline: pd.DataFrame,
    current_year_rows: pd.DataFrame,
    current_year: int,
) -> pd.DataFrame:
    """Keep all prior years and fully replace the selected current year."""

    current = current_year_rows.copy()
    if not current.empty:
        years = set(
            pd.to_numeric(current["calendar_year"], errors="coerce")
            .dropna()
            .astype(int)
        )
        if years != {current_year}:
            raise ValueError(
                "API normalization returned years outside "
                f"{current_year}: {sorted(years)}"
            )
    historical = baseline.loc[
        ~pd.to_numeric(baseline["calendar_year"], errors="coerce").eq(current_year)
    ].copy()
    merged = pd.concat(
        [historical[CROP_WEEKLY_COLUMNS], current[CROP_WEEKLY_COLUMNS]],
        ignore_index=True,
    )
    merged["calendar_year"] = pd.to_numeric(
        merged["calendar_year"], errors="raise"
    ).astype("Int64")
    merged["week_ending"] = pd.to_datetime(merged["week_ending"], errors="coerce")
    merged["value_pct"] = pd.to_numeric(
        merged["value_pct"], errors="coerce"
    ).astype("Float64")
    merged["is_derived"] = merged["is_derived"].astype(bool)
    return merged.sort_values(
        ["metric", "calendar_year", "geography_level", "region_code", "week_ending"],
        na_position="last",
    ).reset_index(drop=True)


def _max_week(frame: pd.DataFrame) -> pd.Timestamp | None:
    values = pd.to_datetime(frame["week_ending"], errors="coerce").dropna()
    return pd.Timestamp(values.max()) if not values.empty else None


def _latest_of_pair(
    progress: pd.DataFrame, condition: pd.DataFrame
) -> pd.Timestamp | None:
    values = [value for value in (_max_week(progress), _max_week(condition)) if value]
    return max(values) if values else None


def _validate_page_metrics(
    progress: pd.DataFrame,
    condition: pd.DataFrame,
    display_config: Path,
) -> dict[str, Any]:
    config = load_display_config(display_config)
    expected = {definition.metric for definition in metric_definitions(config)}
    actual = set(progress["metric"].dropna().astype(str)) | set(
        condition["metric"].dropna().astype(str)
    )
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"Page core metric mapping is incomplete: {missing}")
    return {
        "expected_metrics": sorted(expected),
        "missing_metrics": missing,
        "complete": True,
    }


def validate_candidate_pair(
    *,
    baseline_progress: pd.DataFrame,
    baseline_condition: pd.DataFrame,
    candidate_progress: pd.DataFrame,
    candidate_condition: pd.DataFrame,
    current_year: int,
    retrieved_at_utc: str,
    raw_snapshot: str,
    duplicate_counts: dict[str, int],
    display_config: Path,
) -> dict[str, Any]:
    """Run complete candidate checks before either formal file is switched."""

    prior_results: dict[str, bool] = {}
    for family, baseline, candidate in (
        ("progress", baseline_progress, candidate_progress),
        ("condition", baseline_condition, candidate_condition),
    ):
        old_prior = baseline.loc[
            ~pd.to_numeric(baseline["calendar_year"], errors="coerce").eq(current_year)
        ]
        new_prior = candidate.loc[
            ~pd.to_numeric(candidate["calendar_year"], errors="coerce").eq(current_year)
        ]
        unchanged = _record_counter(
            old_prior, CROP_WEEKLY_COLUMNS
        ) == _record_counter(new_prior, CROP_WEEKLY_COLUMNS)
        prior_results[family] = unchanged
        if not unchanged:
            raise ValueError(f"{family} data before {current_year} changed")

    old_max = {
        "progress": _max_week(baseline_progress),
        "condition": _max_week(baseline_condition),
    }
    new_max = {
        "progress": _max_week(candidate_progress),
        "condition": _max_week(candidate_condition),
    }
    for family in ("progress", "condition"):
        if (
            old_max[family] is not None
            and (
                new_max[family] is None
                or new_max[family] < old_max[family]
            )
        ):
            raise ValueError(
                f"{family} candidate latest week retreated from "
                f"{old_max[family]} to {new_max[family]}"
            )

    progress_internal = _with_internal_source(candidate_progress, "candidate-progress")
    condition_internal = _with_internal_source(
        candidate_condition, "candidate-condition"
    )
    quality = analyze_crop_weekly_quality(
        progress_internal,
        condition_internal,
        exact_duplicate_counts=duplicate_counts,
    )
    blockers = {
        name: int(quality[name])
        for name in BLOCKING_QUALITY_COUNTS
        if int(quality[name])
    }
    state_us_total = pd.concat(
        [candidate_progress, candidate_condition], ignore_index=True
    ).loc[
        lambda data: data["geography_level"].eq("STATE")
        & data["region_name"].eq("US TOTAL")
    ]
    if not state_us_total.empty:
        blockers["state_rows_named_us_total"] = len(state_us_total)
    if blockers:
        raise ValueError(f"Candidate quality checks failed: {blockers}")

    current_rows = pd.concat(
        [
            candidate_progress.loc[
                pd.to_numeric(
                    candidate_progress["calendar_year"], errors="coerce"
                ).eq(current_year)
            ],
            candidate_condition.loc[
                pd.to_numeric(
                    candidate_condition["calendar_year"], errors="coerce"
                ).eq(current_year)
            ],
        ],
        ignore_index=True,
    )
    if not current_rows.empty:
        retrieved_values = set(current_rows["retrieved_at_utc"].dropna().astype(str))
        raw_values = set(current_rows["raw_snapshot"].dropna().astype(str))
        if retrieved_values != {retrieved_at_utc} or raw_values != {raw_snapshot}:
            raise ValueError("Candidate files do not belong to the same update run")

    return {
        "passed": True,
        "prior_years_unchanged": prior_results,
        "old_max_week": old_max,
        "new_max_week": new_max,
        "column_schema_complete": (
            list(candidate_progress.columns) == CROP_WEEKLY_COLUMNS
            and list(candidate_condition.columns) == CROP_WEEKLY_COLUMNS
        ),
        "current_year": current_year,
        "current_year_rows": len(current_rows),
        "same_run_provenance": True,
        "page_metric_mapping": _validate_page_metrics(
            candidate_progress, candidate_condition, display_config
        ),
        "quality": quality,
        "blocking_quality_counts": blockers,
        "state_rows_named_us_total": 0,
    }


def _write_and_verify_candidate(
    frame: pd.DataFrame, path: Path
) -> tuple[pd.DataFrame, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame[CROP_WEEKLY_COLUMNS].to_parquet(path, index=False)
    reloaded = pd.read_parquet(path)
    if list(reloaded.columns) != CROP_WEEKLY_COLUMNS:
        raise ValueError(f"Candidate Parquet column verification failed: {path}")
    if len(reloaded) != len(frame):
        raise ValueError(f"Candidate Parquet row verification failed: {path}")
    return reloaded, _sha256(path)


def replace_processed_pair(
    *,
    candidate_progress: Path,
    candidate_condition: Path,
    target_progress: Path,
    target_condition: Path,
    backup_dir: Path,
) -> dict[str, Any]:
    """Switch both stable Parquets and restore both old files on any failure."""

    candidates = (candidate_progress, candidate_condition)
    targets = (target_progress, target_condition)
    target_existed = {target: target.exists() for target in targets}
    if target_existed[target_progress] != target_existed[target_condition]:
        raise ValueError("Cannot replace a partial stable Processed pair")

    backup_paths: dict[Path, Path] = {}
    if target_existed[target_progress]:
        backup_dir.mkdir(parents=True, exist_ok=False)
        for target in targets:
            backup = backup_dir / target.name
            shutil.copy2(target, backup)
            backup_paths[target] = backup

    token = uuid.uuid4().hex
    staged: dict[Path, Path] = {}
    try:
        for candidate, target in zip(candidates, targets, strict=True):
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.parent / f".{target.stem}.{token}.tmp.parquet"
            staged[target] = stage
            shutil.copy2(candidate, stage)
            if _sha256(stage) != _sha256(candidate):
                raise ValueError("Candidate copy SHA-256 mismatch before switch")
            pd.read_parquet(stage)
    except Exception:
        for stage in staged.values():
            stage.unlink(missing_ok=True)
        raise

    replaced: list[Path] = []
    try:
        for target in targets:
            os.replace(staged[target], target)
            replaced.append(target)
        for candidate, target in zip(candidates, targets, strict=True):
            if _sha256(target) != _sha256(candidate):
                raise ValueError("Formal Processed SHA-256 mismatch after switch")
            pd.read_parquet(target)
    except Exception as exc:
        restore_errors: list[str] = []
        for target in targets:
            try:
                if target_existed[target]:
                    restore_stage = (
                        target.parent / f".{target.stem}.{token}.restore.parquet"
                    )
                    shutil.copy2(backup_paths[target], restore_stage)
                    os.replace(restore_stage, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as restore_exc:  # pragma: no cover - catastrophic FS fault
                restore_errors.append(f"{target}: {restore_exc}")
        if restore_errors:
            raise RuntimeError(
                "Processed switch failed and pair rollback was incomplete: "
                + "; ".join(restore_errors)
            ) from exc
        raise
    finally:
        for stage in staged.values():
            stage.unlink(missing_ok=True)

    return {
        "backup_dir": str(backup_dir) if backup_paths else None,
        "backup_files": {
            target.name: {
                "path": str(backup),
                "sha256": _sha256(backup),
            }
            for target, backup in backup_paths.items()
        },
        "replaced_files": [str(path) for path in replaced],
        "rollback_available": bool(backup_paths),
    }


def _manifest_requests(
    request_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "query_params",
        "http_status",
        "record_count",
        "retry_count",
        "fetched_at_utc",
        "response_sha256",
        "error",
    )
    return [{field: item.get(field) for field in fields} for item in request_items]


def _render_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# USDA NASS 美豆周度自动更新审计",
        "",
        f"- 状态：`{audit['status']}`",
        f"- 运行模式：`{audit['run_mode']}`",
        f"- 当前年度：`{audit['current_year']}`",
        f"- Git提交：`{audit['git_head']}`",
        f"- 开始时间（UTC）：`{audit['started_at_utc']}`",
        f"- 结束时间（UTC）：`{audit.get('finished_at_utc')}`",
        f"- 是否发现业务变化：`{str(audit.get('business_change_found', False)).lower()}`",
        f"- 是否正式发布：`{str(audit.get('published', False)).lower()}`",
        f"- 是否建议发布：`{str(audit.get('recommended_to_publish', False)).lower()}`",
        "",
        "## 记录统计",
        "",
        (
            f"- Progress：{audit.get('progress_old_rows')} "
            f"→ {audit.get('progress_new_rows')}"
        ),
        (
            f"- Condition：{audit.get('condition_old_rows')} "
            f"→ {audit.get('condition_new_rows')}"
        ),
        f"- 新增：{audit.get('added_records', 0)}",
        f"- 修正：{audit.get('corrected_records', 0)}",
        f"- 删除：{audit.get('deleted_records', 0)}",
        "",
        "## 文件",
        "",
        f"- Raw：`{audit.get('raw_path')}`",
        f"- Manifest：`{audit.get('manifest_path')}`",
        f"- Candidate Progress：`{audit.get('candidate_progress_path')}`",
        f"- Candidate Condition：`{audit.get('candidate_condition_path')}`",
        f"- Stable Progress：`{audit.get('processed_progress_path')}`",
        f"- Stable Condition：`{audit.get('processed_condition_path')}`",
    ]
    if audit.get("error"):
        lines.extend(["", "## 错误", "", str(audit["error"])])
    return "\n".join(lines) + "\n"


def _status_payload(audit: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "started_at_utc",
        "finished_at_utc",
        "current_year",
        "old_latest_week",
        "new_latest_week",
        "progress_old_rows",
        "progress_new_rows",
        "condition_old_rows",
        "condition_new_rows",
        "added_records",
        "corrected_records",
        "deleted_records",
        "stable_files_initialized",
        "raw_path",
        "manifest_path",
        "processed_progress_path",
        "processed_condition_path",
        "processed_sha256",
        "git_head",
        "run_mode",
        "business_change_found",
        "published",
        "recommended_to_publish",
        "error",
    )
    return {key: audit.get(key) for key in keys}


def _finish_reports(
    *,
    audit: dict[str, Any],
    audit_json: Path,
    audit_markdown: Path,
    status_path: Path,
    dry_run: bool,
    secret: str,
) -> None:
    audit["audit_json_path"] = str(audit_json)
    audit["audit_markdown_path"] = str(audit_markdown)
    json_text = _json_text(audit)
    markdown = _render_markdown(audit)
    if secret and (secret in json_text or secret in markdown):
        raise ValueError("API key redaction check failed for audit output")
    _atomic_write_text(audit_json, json_text)
    _atomic_write_text(audit_markdown, markdown)
    if not dry_run:
        status_text = _json_text(_status_payload(audit))
        if secret and secret in status_text:
            raise ValueError("API key redaction check failed for status output")
        _atomic_write_text(status_path, status_text)


def _base_audit(
    *,
    run_id: str,
    started_at: datetime,
    current_year: int,
    reporting_date: date,
    dry_run: bool,
    force: bool,
    project_root: Path,
    paths: Mapping[str, Path],
    git_head: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "started",
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": None,
        "current_year": current_year,
        "new_york_reporting_date": reporting_date.isoformat(),
        "season_window": "04-01 through 11-30 (America/New_York)",
        "force": force,
        "run_mode": "dry_run" if dry_run else "publish",
        "api_endpoint": NASS_API_ENDPOINT,
        "git_head": git_head,
        "business_comparison_excluded_fields": sorted(PROVENANCE_COLUMNS),
        "old_latest_week": None,
        "new_latest_week": None,
        "progress_old_rows": None,
        "progress_new_rows": None,
        "condition_old_rows": None,
        "condition_new_rows": None,
        "added_records": 0,
        "corrected_records": 0,
        "deleted_records": 0,
        "stable_files_initialized": False,
        "raw_path": None,
        "manifest_path": None,
        "candidate_progress_path": None,
        "candidate_condition_path": None,
        "processed_progress_path": _relative(
            paths["stable_progress"], project_root
        ),
        "processed_condition_path": _relative(
            paths["stable_condition"], project_root
        ),
        "processed_sha256": {},
        "business_change_found": False,
        "published": False,
        "recommended_to_publish": False,
        "error": None,
    }


def run_soybean_crop_weekly_update(
    *,
    project_root: Path = PROJECT_ROOT,
    dry_run: bool = False,
    force: bool = False,
    timeout: int = 60,
    retries: int = 2,
    transport: Transport | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] | None = None,
    env: Mapping[str, str] | None = None,
    git_head: str | None = None,
) -> dict[str, Any]:
    """Fetch one current year, validate a complete pair, and publish safely."""

    environment = os.environ if env is None else env
    deployment_git_head = resolve_deployment_git_head(
        project_root,
        explicit_git_head=git_head,
        env=environment,
    )
    started_at = _utc_now(now)
    reporting_date = new_york_reporting_date(started_at)
    current_year = reporting_date.year
    timestamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{current_year}-{timestamp}-{uuid.uuid4().hex[:8]}"
    paths = soybean_weekly_update_paths(project_root)
    audit_subdir = "dry_runs" if dry_run else "updates"
    audit_dir = paths["audit_root"] / audit_subdir
    audit_json = audit_dir / f"soybeans_crop_weekly_update_{run_id}.json"
    audit_markdown = audit_dir / f"soybeans_crop_weekly_update_{run_id}.md"
    raw_target = (
        paths["raw_dir"]
        / f"nass_soybeans_crop_weekly_{current_year}_{timestamp}.json"
    )
    manifest_target = (
        paths["raw_dir"]
        / f"nass_soybeans_crop_weekly_{current_year}_{timestamp}_manifest.json"
    )
    logger = _make_logger(
        paths["log_dir"] / f"nass_soybeans_crop_weekly_update_{started_at:%Y%m%d}.log"
    )
    audit = _base_audit(
        run_id=run_id,
        started_at=started_at,
        current_year=current_year,
        reporting_date=reporting_date,
        dry_run=dry_run,
        force=force,
        project_root=project_root,
        paths=paths,
        git_head=deployment_git_head,
    )

    def finish(status: str, *, secret: str = "") -> dict[str, Any]:
        audit["status"] = status
        audit["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _finish_reports(
            audit=audit,
            audit_json=audit_json,
            audit_markdown=audit_markdown,
            status_path=paths["status"],
            dry_run=dry_run,
            secret=secret,
        )
        return audit

    try:
        logger.info(
            "Resolved deployment Git commit git_head=%s",
            deployment_git_head,
        )
        if not force and not is_soybean_reporting_season(reporting_date):
            logger.info(
                "Skipped outside season current_year=%s reporting_date=%s",
                current_year,
                reporting_date,
            )
            return finish("out_of_season")

        secret = str(environment.get("NASS_API_KEY", ""))
        if not secret:
            audit["error"] = "缺少 NASS_API_KEY；没有调用 USDA API。"
            finish("failed")
            raise SoybeanWeeklyUpdateError(audit["error"])

        request_items: list[dict[str, Any]] = []
        fetch_error: Exception | None = None
        planned_queries = build_soybean_crop_weekly_current_year_query_grid(
            current_year
        )
        for query_index, query in enumerate(planned_queries):
            try:
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
                    "error": None,
                }
                request_items.append(item)
                logger.info(
                    "Fetched year=%s category=%s level=%s status=%s "
                    "records=%s retries=%s",
                    current_year,
                    query["statisticcat_desc"],
                    query["agg_level_desc"],
                    item["http_status"],
                    item["record_count"],
                    item["retry_count"],
                )
            except Exception as exc:
                safe_error = redact_secret(
                    f"{type(exc).__name__}: {exc}", secret
                )
                request_items.append(
                    {
                        "query_params": query,
                        "http_status": getattr(exc, "http_status", None),
                        "record_count": 0,
                        "retry_count": getattr(exc, "retry_count", retries),
                        "fetched_at_utc": None,
                        "response_sha256": None,
                        "response": None,
                        "error": safe_error,
                    }
                )
                for pending in planned_queries[query_index + 1 :]:
                    request_items.append(
                        {
                            "query_params": pending,
                            "http_status": None,
                            "record_count": 0,
                            "retry_count": 0,
                            "fetched_at_utc": None,
                            "response_sha256": None,
                            "response": None,
                            "error": "not_attempted_after_request_failure",
                        }
                    )
                fetch_error = exc
                break

        raw_payload = {
            "schema_version": 1,
            "api_endpoint": NASS_API_ENDPOINT,
            "run_id": run_id,
            "retrieved_at_utc": started_at.isoformat(),
            "current_year": current_year,
            "requests": [
                {
                    "query_params": item["query_params"],
                    "http_status": item["http_status"],
                    "record_count": item["record_count"],
                    "retry_count": item["retry_count"],
                    "fetched_at_utc": item["fetched_at_utc"],
                    "response_sha256": item["response_sha256"],
                    "response": item["response"],
                    "error": item["error"],
                }
                for item in request_items
            ],
        }
        audit["requests"] = _manifest_requests(request_items)
        audit["request_record_counts"] = {
            (
                f"{item['query_params']['statisticcat_desc']}/"
                f"{item['query_params']['agg_level_desc']}"
            ): item["record_count"]
            for item in request_items
        }
        audit["raw_path"] = _relative(raw_target, project_root)
        audit["manifest_path"] = _relative(manifest_target, project_root)

        if fetch_error is not None:
            safe_error = redact_secret(
                f"{type(fetch_error).__name__}: {fetch_error}", secret
            )
            audit["error"] = safe_error
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "run_mode": audit["run_mode"],
                "current_year": current_year,
                "started_at_utc": audit["started_at_utc"],
                "requests": audit["requests"],
                "git_head": audit["git_head"],
                "status": "failed",
                "business_change_found": False,
                "published": False,
                "error": safe_error,
            }
            raw_sha, manifest_sha = _write_immutable_json_pair(
                raw_target,
                raw_payload,
                manifest_target,
                manifest,
                secret=secret,
            )
            audit["raw_sha256"] = raw_sha
            audit["manifest_sha256"] = manifest_sha
            finish("failed", secret=secret)
            raise SoybeanWeeklyUpdateError(safe_error) from fetch_error

        total_records = sum(item["record_count"] for item in request_items)
        if total_records == 0:
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "run_mode": audit["run_mode"],
                "current_year": current_year,
                "started_at_utc": audit["started_at_utc"],
                "requests": audit["requests"],
                "git_head": audit["git_head"],
                "status": "no_data",
                "business_change_found": False,
                "published": False,
                "error": None,
            }
            raw_sha, manifest_sha = _write_immutable_json_pair(
                raw_target,
                raw_payload,
                manifest_target,
                manifest,
                secret=secret,
            )
            audit["raw_sha256"] = raw_sha
            audit["manifest_sha256"] = manifest_sha
            logger.info("No current-year USDA records; formal files preserved")
            return finish("no_data", secret=secret)

        raw_relative = str(audit["raw_path"])
        try:
            baseline_progress, baseline_condition, initialized, baseline_source = (
                _read_baseline_pair(paths)
            )
            audit["stable_files_initialized"] = initialized
            audit["baseline_source"] = baseline_source
            audit["progress_old_rows"] = len(baseline_progress)
            audit["condition_old_rows"] = len(baseline_condition)
            audit["old_latest_week"] = _latest_of_pair(
                baseline_progress, baseline_condition
            )

            successful_requests = [
                {
                    "query_params": item["query_params"],
                    "response": item["response"],
                    "record_count": item["record_count"],
                }
                for item in request_items
                if item["response"] is not None
            ]
            mapping = identify_crop_metric_mappings(
                successful_requests, require_all=False
            )
            current_progress, direct_condition, duplicate_counts = (
                normalize_crop_weekly_rows(
                    successful_requests,
                    mapping_audit=mapping,
                    retrieved_at_utc=started_at.isoformat(),
                    raw_snapshot=raw_relative,
                    allow_empty_families=True,
                )
            )
            current_condition, derivation = derive_good_excellent(direct_condition)

            for family, baseline, current in (
                ("PROGRESS", baseline_progress, current_progress),
                ("CONDITION", baseline_condition, current_condition),
            ):
                old_current_count = int(
                    pd.to_numeric(
                        baseline["calendar_year"], errors="coerce"
                    ).eq(current_year).sum()
                )
                if old_current_count and current.empty:
                    raise ValueError(
                        f"{family} API result is empty but the baseline already has "
                        f"{old_current_count} current-year rows"
                    )

            candidate_progress_internal = merge_current_year(
                _with_internal_source(baseline_progress, "baseline-progress"),
                current_progress,
                current_year,
            )
            candidate_condition_internal = merge_current_year(
                _with_internal_source(baseline_condition, "baseline-condition"),
                current_condition,
                current_year,
            )
            candidate_progress = candidate_progress_internal[CROP_WEEKLY_COLUMNS]
            candidate_condition = candidate_condition_internal[CROP_WEEKLY_COLUMNS]

            candidate_dir = paths["candidate_root"] / run_id
            candidate_progress_path = (
                candidate_dir / "soybeans_crop_progress_weekly.parquet"
            )
            candidate_condition_path = (
                candidate_dir / "soybeans_crop_condition_weekly.parquet"
            )
            reloaded_progress, progress_candidate_sha = (
                _write_and_verify_candidate(
                    candidate_progress, candidate_progress_path
                )
            )
            reloaded_condition, condition_candidate_sha = (
                _write_and_verify_candidate(
                    candidate_condition, candidate_condition_path
                )
            )
            audit["candidate_progress_path"] = _relative(
                candidate_progress_path, project_root
            )
            audit["candidate_condition_path"] = _relative(
                candidate_condition_path, project_root
            )
            audit["candidate_sha256"] = {
                "progress": progress_candidate_sha,
                "condition": condition_candidate_sha,
            }

            validation = validate_candidate_pair(
                baseline_progress=baseline_progress,
                baseline_condition=baseline_condition,
                candidate_progress=reloaded_progress,
                candidate_condition=reloaded_condition,
                current_year=current_year,
                retrieved_at_utc=started_at.isoformat(),
                raw_snapshot=raw_relative,
                duplicate_counts=duplicate_counts,
                display_config=paths["display_config"],
            )
            progress_diff = compare_business_records(
                baseline_progress, reloaded_progress
            )
            condition_diff = compare_business_records(
                baseline_condition, reloaded_condition
            )
            audit["mapping"] = mapping
            audit["good_excellent"] = derivation
            audit["candidate_validation"] = validation
            audit["business_changes"] = {
                "progress": progress_diff,
                "condition": condition_diff,
            }
            audit["progress_new_rows"] = len(reloaded_progress)
            audit["condition_new_rows"] = len(reloaded_condition)
            audit["new_latest_week"] = _latest_of_pair(
                reloaded_progress, reloaded_condition
            )
            audit["added_records"] = int(
                progress_diff["added"] + condition_diff["added"]
            )
            audit["corrected_records"] = int(
                progress_diff["corrected"] + condition_diff["corrected"]
            )
            audit["deleted_records"] = int(
                progress_diff["deleted"] + condition_diff["deleted"]
            )
            audit["business_change_found"] = bool(
                progress_diff["business_changed"]
                or condition_diff["business_changed"]
            )
            audit["recommended_to_publish"] = bool(
                initialized or audit["business_change_found"]
            )

            if dry_run:
                outcome = "initialized" if initialized else (
                    "updated" if audit["business_change_found"] else "no_change"
                )
                audit["dry_run_outcome"] = outcome
                status = outcome
            elif not initialized and not audit["business_change_found"]:
                status = "no_change"
                audit["processed_sha256"] = {
                    "progress": _sha256(paths["stable_progress"]),
                    "condition": _sha256(paths["stable_condition"]),
                }
            else:
                backup_dir = paths["backup_root"] / timestamp
                publish_audit = replace_processed_pair(
                    candidate_progress=candidate_progress_path,
                    candidate_condition=candidate_condition_path,
                    target_progress=paths["stable_progress"],
                    target_condition=paths["stable_condition"],
                    backup_dir=backup_dir,
                )
                formal_progress = pd.read_parquet(paths["stable_progress"])
                formal_condition = pd.read_parquet(paths["stable_condition"])
                if len(formal_progress) != len(reloaded_progress) or len(
                    formal_condition
                ) != len(reloaded_condition):
                    raise ValueError("Post-publish Processed row verification failed")
                if _max_week(formal_progress) != _max_week(
                    reloaded_progress
                ) or _max_week(formal_condition) != _max_week(reloaded_condition):
                    raise ValueError("Post-publish latest-week verification failed")
                audit["processed_sha256"] = {
                    "progress": _sha256(paths["stable_progress"]),
                    "condition": _sha256(paths["stable_condition"]),
                }
                if audit["processed_sha256"] != audit["candidate_sha256"]:
                    raise ValueError(
                        "Post-publish candidate SHA-256 verification failed"
                    )
                audit["publish"] = publish_audit
                audit["published"] = True
                status = "initialized" if initialized else "updated"

            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "run_mode": audit["run_mode"],
                "current_year": current_year,
                "started_at_utc": audit["started_at_utc"],
                "requests": audit["requests"],
                "raw_snapshot": audit["raw_path"],
                "git_head": audit["git_head"],
                "status": status,
                "business_change_found": audit["business_change_found"],
                "published": audit["published"],
                "recommended_to_publish": audit["recommended_to_publish"],
                "error": None,
            }
            raw_sha, manifest_sha = _write_immutable_json_pair(
                raw_target,
                raw_payload,
                manifest_target,
                manifest,
                secret=secret,
            )
            audit["raw_sha256"] = raw_sha
            audit["manifest_sha256"] = manifest_sha
            logger.info(
                "Update finished status=%s old_rows=%s/%s new_rows=%s/%s "
                "added=%s corrected=%s deleted=%s published=%s",
                status,
                audit["progress_old_rows"],
                audit["condition_old_rows"],
                audit["progress_new_rows"],
                audit["condition_new_rows"],
                audit["added_records"],
                audit["corrected_records"],
                audit["deleted_records"],
                audit["published"],
            )
            return finish(status, secret=secret)
        except Exception as exc:
            safe_error = redact_secret(f"{type(exc).__name__}: {exc}", secret)
            audit["error"] = safe_error
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "run_mode": audit["run_mode"],
                "current_year": current_year,
                "started_at_utc": audit["started_at_utc"],
                "requests": audit["requests"],
                "raw_snapshot": audit["raw_path"],
                "git_head": audit["git_head"],
                "status": "failed",
                "business_change_found": audit["business_change_found"],
                "published": audit["published"],
                "error": safe_error,
            }
            if not raw_target.exists() and not manifest_target.exists():
                raw_sha, manifest_sha = _write_immutable_json_pair(
                    raw_target,
                    raw_payload,
                    manifest_target,
                    manifest,
                    secret=secret,
                )
                audit["raw_sha256"] = raw_sha
                audit["manifest_sha256"] = manifest_sha
            logger.error("Current-year update failed safely: %s", safe_error)
            finish("failed", secret=secret)
            raise SoybeanWeeklyUpdateError(safe_error) from exc
    finally:
        for handler in list(logger.handlers):
            handler.flush()
            handler.close()
            logger.removeHandler(handler)

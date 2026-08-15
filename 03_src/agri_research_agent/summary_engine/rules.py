from __future__ import annotations

from functools import lru_cache
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "02_configs" / "summary_rules_v1.yaml"


@lru_cache(maxsize=8)
def _load_summary_rules_versioned(
    path: str, size: int, mtime_ns: int,
) -> dict[str, Any]:
    del size, mtime_ns
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not payload.get("rule_version"):
        raise ValueError("Summary规则配置版本无效")
    return payload


def load_summary_rules(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load rules with file-identity invalidation instead of a fixed TTL."""

    resolved = Path(path).resolve()
    stat = resolved.stat()
    return _load_summary_rules_versioned(str(resolved), stat.st_size, stat.st_mtime_ns)


def freshness_status(module: str, source_date: str | None, rules: dict[str, Any] | None = None, *, today: date | None = None) -> str:
    if not source_date:
        return "missing"
    threshold = (rules or load_summary_rules()).get("freshness_days", {}).get(module)
    if threshold is None:
        return "unknown"
    try:
        parsed = datetime.strptime(source_date, "%Y-%m").date().replace(day=1) if len(source_date) == 7 else datetime.fromisoformat(source_date.replace("Z", "+00:00")).date()
    except ValueError:
        return "unknown"
    age = ((today or datetime.now(timezone.utc).date()) - parsed).days
    return "fresh" if age <= int(threshold) else "stale"


def classify_rainfall(relative_pct: float, absolute_mm: float, rules: dict[str, Any] | None = None) -> str:
    cfg = (rules or load_summary_rules())["rainfall"]
    if abs(absolute_mm) < float(cfg["absolute_guard_mm"]):
        return "rainfall_near_normal_absolute_guard"
    magnitude = abs(relative_pct); direction = "above" if relative_pct > 0 else "below"
    if magnitude <= float(cfg["neutral_pct"]): return "rainfall_near_normal"
    if magnitude <= float(cfg["moderate_pct"]): return f"rainfall_{direction}_normal"
    if magnitude < float(cfg["strong_pct"]): return f"rainfall_clearly_{direction}_normal"
    return f"rainfall_significantly_{direction}_normal"


def classify_temperature(anomaly_c: float, rules: dict[str, Any] | None = None) -> str:
    cfg = (rules or load_summary_rules())["temperature_max"]
    magnitude = abs(anomaly_c); direction = "above" if anomaly_c > 0 else "below"
    if magnitude <= float(cfg["neutral_c"]): return "temperature_near_normal"
    if magnitude < float(cfg["moderate_c"]): return f"temperature_{direction}_normal"
    if magnitude < float(cfg["strong_c"]): return f"temperature_clearly_{direction}_normal"
    return f"temperature_significantly_{direction}_normal"


def classify_model_consistency(ec_pct: float, gfs_pct: float, rules: dict[str, Any] | None = None) -> str:
    neutral = float((rules or load_summary_rules())["rainfall"]["neutral_pct"])
    close = float((rules or load_summary_rules())["model_consistency"]["close_difference_pct_points"])
    ec_neutral, gfs_neutral = abs(ec_pct) <= neutral, abs(gfs_pct) <= neutral
    if ec_neutral and gfs_neutral: return "models_same_direction_close_intensity"
    if ec_neutral != gfs_neutral: return "one_neutral_one_directional"
    if ec_pct * gfs_pct < 0: return "models_opposite_direction"
    return "models_same_direction_close_intensity" if abs(ec_pct - gfs_pct) <= close else "models_same_direction_different_intensity"

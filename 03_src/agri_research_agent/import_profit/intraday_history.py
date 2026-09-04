"""Unified legacy/PM history for soybean board crush-margin charts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from agri_research_agent.market_data.intraday import MarketSession

from .intraday_store import (
    ResolvedIntradayProfitRelease,
    list_intraday_profit_batches,
)


class IntradayProfitHistoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PmSeasonalChartSpec:
    shipment_month: int
    domestic_contract_month: str
    window_months: tuple[int, ...]
    cross_year: bool

    @property
    def title(self) -> str:
        return (
            f"大豆盘面榨利：{self.shipment_month}月对"
            f"{self.domestic_contract_month}"
        )


@dataclass(frozen=True, slots=True)
class PmSeasonalPoint:
    trade_date: date
    series_year: int
    shipment_year: int
    shipment_month: int
    shipment_period: str
    soymeal_contract: str
    soyoil_contract: str
    domestic_contract: str
    net_crush_margin_cny_per_tonne: float
    season_position: int
    release_id: str
    history_source: str = "pm"


@dataclass(frozen=True, slots=True)
class PmSeasonalSeries:
    series_year: int
    points: tuple[PmSeasonalPoint, ...]


@dataclass(frozen=True, slots=True)
class PmSeasonalChart:
    spec: PmSeasonalChartSpec
    series: tuple[PmSeasonalSeries, ...]

    @property
    def point_count(self) -> int:
        return sum(len(item.points) for item in self.series)


@dataclass(frozen=True, slots=True)
class SeasonalHistoryAudit:
    legacy_min_trade_date: date | None
    legacy_max_trade_date: date | None
    legacy_point_count: int
    pm_min_trade_date: date | None
    pm_max_trade_date: date | None
    pm_point_count: int
    pm_cutover_date: date | None
    merged_point_count: int
    legacy_overlap_removed: int
    legacy_post_cutover_excluded: int


@dataclass(frozen=True, slots=True)
class UnifiedSeasonalHistory:
    charts: tuple[PmSeasonalChart, ...]
    audit: SeasonalHistoryAudit


@dataclass(frozen=True, slots=True)
class _HistoryObservation:
    trade_date: date
    origin: str
    shipment_year: int
    shipment_month: int
    shipment_period: str
    net_crush_margin_cny_per_tonne: float
    soymeal_contract: str
    soyoil_contract: str
    release_id: str
    history_source: str


PM_SEASONAL_CHART_SPECS = (
    PmSeasonalChartSpec(1, "01", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(2, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(3, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(4, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(5, "05", (5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3, 4), True),
    PmSeasonalChartSpec(6, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(7, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(8, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(9, "09", tuple(range(1, 10)), False),
    PmSeasonalChartSpec(10, "01", tuple(range(4, 13)), False),
    PmSeasonalChartSpec(11, "01", tuple(range(4, 13)), False),
    PmSeasonalChartSpec(12, "01", tuple(range(4, 13)), False),
)


def load_unified_seasonal_history(
    legacy_result_path: str | Path,
    *,
    legacy_source_sha256: str,
    result_root: str | Path,
    origin: str,
    expected_environment: str | None = None,
    snapshot_root: str | Path | None = None,
) -> UnifiedSeasonalHistory:
    """Read each source once, apply the PM cutover, then build twelve charts."""

    batches = list_intraday_profit_batches(result_root, MarketSession.PM,
        expected_environment=expected_environment, snapshot_root=snapshot_root)
    legacy = _load_legacy_observations(
        legacy_result_path,
        source_sha256=legacy_source_sha256,
        origin=origin,
    )
    pm = _pm_observations(batches, origin=origin)
    cutover = min((batch.business_date for batch in batches), default=None)
    return merge_seasonal_history(
        legacy,
        pm,
        origin=origin,
        pm_cutover_date=cutover,
    )


def load_pm_seasonal_charts(
    result_root: str | Path,
    *,
    origin: str,
) -> tuple[PmSeasonalChart, ...]:
    """Compatibility loader for formal PM-only chart history."""

    batches = list_intraday_profit_batches(result_root, MarketSession.PM)
    return build_pm_seasonal_charts(batches, origin=origin)


def build_pm_seasonal_charts(
    batches: tuple[ResolvedIntradayProfitRelease, ...],
    *,
    origin: str,
) -> tuple[PmSeasonalChart, ...]:
    """Transform formal PM result rows without synthesizing missing dates."""

    observations = _pm_observations(batches, origin=origin)
    return _build_charts(observations, origin=origin)


def merge_seasonal_history(
    legacy: tuple[_HistoryObservation, ...],
    pm: tuple[_HistoryObservation, ...],
    *,
    origin: str,
    pm_cutover_date: date | None,
) -> UnifiedSeasonalHistory:
    """Merge at source grain; PM wins and no point is filled or interpolated."""

    _require_origin(origin)
    legacy_by_key = _unique_by_key(legacy, source="legacy", origin=origin)
    pm_by_key = _unique_by_key(pm, source="pm", origin=origin)
    if pm_cutover_date is None:
        if pm_by_key:
            raise IntradayProfitHistoryError(
                "PM observations require a PM cutover date"
            )
        merged = dict(legacy_by_key)
        post_cutover_excluded = 0
    else:
        if any(key[2] < pm_cutover_date for key in pm_by_key):
            raise IntradayProfitHistoryError(
                "PM history contains a point before its cutover date"
            )
        merged = {
            key: point
            for key, point in legacy_by_key.items()
            if key[2] < pm_cutover_date
        }
        post_cutover_excluded = sum(
            key[2] >= pm_cutover_date for key in legacy_by_key
        )
        merged.update(pm_by_key)
    overlap = len(set(legacy_by_key).intersection(pm_by_key))
    merged_points = tuple(
        merged[key]
        for key in sorted(merged, key=lambda item: (item[2], item[0], item[1]))
    )
    audit = SeasonalHistoryAudit(
        legacy_min_trade_date=_min_date(legacy_by_key.values()),
        legacy_max_trade_date=_max_date(legacy_by_key.values()),
        legacy_point_count=len(legacy_by_key),
        pm_min_trade_date=_min_date(pm_by_key.values()),
        pm_max_trade_date=_max_date(pm_by_key.values()),
        pm_point_count=len(pm_by_key),
        pm_cutover_date=pm_cutover_date,
        merged_point_count=len(merged_points),
        legacy_overlap_removed=overlap,
        legacy_post_cutover_excluded=post_cutover_excluded,
    )
    return UnifiedSeasonalHistory(
        charts=_build_charts(merged_points, origin=origin),
        audit=audit,
    )


def _load_legacy_observations(
    path: str | Path,
    *,
    source_sha256: str,
    origin: str,
) -> tuple[_HistoryObservation, ...]:
    _require_origin(origin)
    source = Path(path)
    if not source.is_file():
        raise IntradayProfitHistoryError(
            "legacy historical result source is unavailable"
        )
    normalized_sha = source_sha256.upper()
    if len(normalized_sha) != 64 or any(
        character not in "0123456789ABCDEF" for character in normalized_sha
    ):
        raise IntradayProfitHistoryError(
            "legacy historical result identity is invalid"
        )
    shipment_months = [spec.shipment_month for spec in PM_SEASONAL_CHART_SPECS]
    columns = [
        "business_date",
        "origin",
        "shipment_year",
        "shipment_month",
        "shipment_period",
        "net_crush_margin_cny_per_tonne",
        "calculation_status",
    ]
    try:
        rows = pq.read_table(
            source,
            columns=columns,
            filters=[
                ("origin", "=", origin),
                ("shipment_month", "in", shipment_months),
            ],
        ).to_pylist()
    except Exception as exc:
        raise IntradayProfitHistoryError(
            "failed to read legacy historical result source"
        ) from exc
    observations = []
    seen_all: set[tuple[str, int, date]] = set()
    for row in rows:
        trade_date = row["business_date"]
        shipment_month = int(row["shipment_month"])
        key = (origin, shipment_month, trade_date)
        if key in seen_all:
            raise IntradayProfitHistoryError(
                "legacy history contains a duplicate trade date/shipment month"
            )
        seen_all.add(key)
        value = row["net_crush_margin_cny_per_tonne"]
        success = str(row["calculation_status"]).lower() == "success"
        if success != (value is not None):
            raise IntradayProfitHistoryError(
                "legacy history calculation status is inconsistent"
            )
        if not success:
            continue
        shipment_year = int(row["shipment_year"])
        shipment_period = str(row["shipment_period"])
        if shipment_period != f"{shipment_year:04d}-{shipment_month:02d}":
            raise IntradayProfitHistoryError(
                "legacy history shipment period is invalid"
            )
        observations.append(
            _HistoryObservation(
                trade_date=trade_date,
                origin=origin,
                shipment_year=shipment_year,
                shipment_month=shipment_month,
                shipment_period=shipment_period,
                net_crush_margin_cny_per_tonne=float(value),
                soymeal_contract="",
                soyoil_contract="",
                release_id=f"legacy:{normalized_sha}",
                history_source="legacy",
            )
        )
    return tuple(observations)


def _pm_observations(
    batches: tuple[ResolvedIntradayProfitRelease, ...],
    *,
    origin: str,
) -> tuple[_HistoryObservation, ...]:
    _require_origin(origin)
    shipment_months = {
        spec.shipment_month for spec in PM_SEASONAL_CHART_SPECS
    }
    observations = []
    for batch in batches:
        if batch.session is not MarketSession.PM:
            raise IntradayProfitHistoryError(
                "seasonal history accepts formal PM releases only"
            )
        for row in batch.rows:
            if row.get("session") != MarketSession.PM.value:
                raise IntradayProfitHistoryError(
                    "PM release contains a non-PM result row"
                )
            if row.get("origin") != origin:
                continue
            shipment_month = int(row["shipment_month"])
            if shipment_month not in shipment_months:
                continue
            trade_date = date.fromisoformat(str(row["business_date"]))
            if trade_date != batch.business_date:
                raise IntradayProfitHistoryError(
                    "PM result row business date differs from its release"
                )
            if (
                row.get("availability_status") != "SUCCESS"
                or str(row.get("calculation_status", "")).lower() != "success"
                or row.get("net_crush_margin_cny_per_tonne") is None
            ):
                continue
            observations.append(
                _HistoryObservation(
                    trade_date=trade_date,
                    origin=origin,
                    shipment_year=int(row["shipment_year"]),
                    shipment_month=shipment_month,
                    shipment_period=str(row["shipment_period"]),
                    net_crush_margin_cny_per_tonne=float(
                        row["net_crush_margin_cny_per_tonne"]
                    ),
                    soymeal_contract=str(row["soymeal_contract"]),
                    soyoil_contract=str(row["soyoil_contract"]),
                    release_id=batch.release_id,
                    history_source="pm",
                )
            )
    _unique_by_key(tuple(observations), source="pm", origin=origin)
    return tuple(observations)


def _build_charts(
    observations: tuple[_HistoryObservation, ...],
    *,
    origin: str,
) -> tuple[PmSeasonalChart, ...]:
    specs = {item.shipment_month: item for item in PM_SEASONAL_CHART_SPECS}
    by_chart: dict[int, dict[int, list[PmSeasonalPoint]]] = {
        month: {} for month in specs
    }
    for observation in observations:
        if observation.origin != origin:
            continue
        spec = specs[observation.shipment_month]
        if observation.trade_date.month not in spec.window_months:
            continue
        series_year = _series_year(observation.trade_date, spec=spec)
        point = PmSeasonalPoint(
            trade_date=observation.trade_date,
            series_year=series_year,
            shipment_year=observation.shipment_year,
            shipment_month=observation.shipment_month,
            shipment_period=observation.shipment_period,
            soymeal_contract=observation.soymeal_contract,
            soyoil_contract=observation.soyoil_contract,
            domestic_contract=_domestic_contract(
                observation.soymeal_contract,
                observation.soyoil_contract,
            ),
            net_crush_margin_cny_per_tonne=(
                observation.net_crush_margin_cny_per_tonne
            ),
            season_position=_season_position(observation.trade_date, spec=spec),
            release_id=observation.release_id,
            history_source=observation.history_source,
        )
        by_chart[observation.shipment_month].setdefault(
            series_year, []
        ).append(point)
    charts = []
    for spec in PM_SEASONAL_CHART_SPECS:
        series = tuple(
            PmSeasonalSeries(
                series_year=year,
                points=tuple(
                    sorted(
                        points,
                        key=lambda item: (
                            item.season_position, item.trade_date
                        ),
                    )
                ),
            )
            for year, points in sorted(by_chart[spec.shipment_month].items())
        )
        charts.append(PmSeasonalChart(spec=spec, series=series))
    return tuple(charts)


def _unique_by_key(
    observations: tuple[_HistoryObservation, ...],
    *,
    source: str,
    origin: str,
) -> dict[tuple[str, int, date], _HistoryObservation]:
    by_key: dict[tuple[str, int, date], _HistoryObservation] = {}
    for observation in observations:
        if observation.history_source != source:
            raise IntradayProfitHistoryError(
                f"{source} history contains an invalid source identity"
            )
        if observation.origin != origin:
            continue
        key = (
            observation.origin,
            observation.shipment_month,
            observation.trade_date,
        )
        if key in by_key:
            raise IntradayProfitHistoryError(
                f"{source} history contains a duplicate trade date/shipment month"
            )
        by_key[key] = observation
    return by_key


def _min_date(observations) -> date | None:
    return min((item.trade_date for item in observations), default=None)


def _max_date(observations) -> date | None:
    return max((item.trade_date for item in observations), default=None)


def _require_origin(origin: str) -> None:
    if not origin or not isinstance(origin, str):
        raise IntradayProfitHistoryError("origin is required")


def _series_year(trade_date: date, *, spec: PmSeasonalChartSpec) -> int:
    if spec.cross_year and trade_date.month >= 5:
        return trade_date.year + 1
    return trade_date.year


def _season_position(
    trade_date: date, *, spec: PmSeasonalChartSpec
) -> int:
    month_index = spec.window_months.index(trade_date.month)
    return month_index * 31 + trade_date.day - 1


def _domestic_contract(soymeal: str, soyoil: str) -> str:
    if not soymeal and not soyoil:
        return "—"
    meal_code = soymeal[1:] if soymeal.startswith("M") else soymeal
    oil_code = soyoil[1:] if soyoil.startswith("Y") else soyoil
    return meal_code if meal_code == oil_code else f"{soymeal} / {soyoil}"


__all__ = [
    "IntradayProfitHistoryError",
    "PM_SEASONAL_CHART_SPECS",
    "PmSeasonalChart",
    "PmSeasonalChartSpec",
    "PmSeasonalPoint",
    "PmSeasonalSeries",
    "SeasonalHistoryAudit",
    "UnifiedSeasonalHistory",
    "build_pm_seasonal_charts",
    "load_pm_seasonal_charts",
    "load_unified_seasonal_history",
    "merge_seasonal_history",
]

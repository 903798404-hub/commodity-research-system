"""Legacy spread-name parsing, display metadata, and board classification."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agri_research_agent.market_data.contracts import ContractId, Exchange

from .models import SpreadCalculation, SpreadDefinition, SpreadLeg


BOARD_OPTIONS = ["豆系月差", "棕榈油与菜系月差", "品种间套利"]
INSTRUMENT_LABELS = {
    "M": "豆粕",
    "Y": "豆油",
    "RM": "菜粕",
    "OI": "菜油",
    "P": "棕榈油",
}
INSTRUMENT_EXCHANGES = {
    "M": Exchange.DCE,
    "Y": Exchange.DCE,
    "P": Exchange.DCE,
    "RM": Exchange.CZCE,
    "OI": Exchange.CZCE,
}
SOY_INSTRUMENTS = {"M", "Y"}
PALM_RAPESEED_INSTRUMENTS = {"P", "OI", "RM"}
OIL_INSTRUMENTS = {"Y", "OI", "P"}
MEAL_INSTRUMENTS = {"M", "RM"}


@dataclass(frozen=True, slots=True)
class ParsedSpreadName:
    instruments: tuple[str, ...]
    months: tuple[int, ...]


def parse_legacy_spread_name(spread_name: str) -> ParsedSpreadName | None:
    name, _, delivery = str(spread_name).partition(" ")
    instruments = tuple(name.split("-"))
    if not delivery:
        return None
    try:
        months = tuple(int(month) for month in delivery.split("-"))
    except ValueError:
        return None
    if (
        not instruments
        or not all(instrument in INSTRUMENT_LABELS for instrument in instruments)
        or not months
        or not all(1 <= month <= 12 for month in months)
    ):
        return None
    return ParsedSpreadName(instruments, months)


def parse_spread_name(spread_name: str) -> tuple[list[str], list[int]] | None:
    """Compatibility shape used by the existing page and its golden tests."""

    parsed = parse_legacy_spread_name(spread_name)
    if parsed is None:
        return None
    return list(parsed.instruments), list(parsed.months)


def parse_season(season: str) -> tuple[int, int] | None:
    parts = str(season).split("/")
    if len(parts) != 2:
        return None
    try:
        start_year, end_year = (int(part) for part in parts)
    except ValueError:
        return None
    if end_year != start_year + 1:
        return None
    return start_year, end_year


def _products_and_delivery_years(
    parsed: ParsedSpreadName,
    season: tuple[int, int],
) -> tuple[tuple[str, str], tuple[int, int], tuple[int, int]]:
    start_year, end_year = season
    if len(parsed.instruments) == 1 and len(parsed.months) == 2:
        products = (parsed.instruments[0], parsed.instruments[0])
        months = (parsed.months[0], parsed.months[1])
        years = (
            (start_year, end_year)
            if months == (9, 1)
            else (end_year, end_year)
        )
        return products, months, years
    if len(parsed.instruments) == 2 and len(parsed.months) == 1:
        month = parsed.months[0]
        year = start_year if month >= 9 else end_year
        return (parsed.instruments[0], parsed.instruments[1]), (month, month), (year, year)
    raise ValueError("legacy spread name does not describe exactly two legs")


def definition_from_legacy(
    spread_name: str,
    season: str,
    calculation: SpreadCalculation = SpreadCalculation.DIFFERENCE,
) -> SpreadDefinition:
    parsed = parse_legacy_spread_name(spread_name)
    parsed_season = parse_season(season)
    if parsed is None:
        raise ValueError(f"unsupported legacy spread name: {spread_name!r}")
    if parsed_season is None:
        raise ValueError(f"unsupported season: {season!r}")
    products, months, years = _products_and_delivery_years(parsed, parsed_season)
    legs = tuple(
        SpreadLeg(ContractId(INSTRUMENT_EXCHANGES[product], product, year, month))
        for product, month, year in zip(products, months, years, strict=True)
    )
    return SpreadDefinition(spread_name, legs[0], legs[1], calculation)


def classify_board(spread_name: str) -> tuple[str, str]:
    name = str(spread_name)
    if "/" in name or any(keyword in name.lower() for keyword in ["压榨", "crush", "ratio"]):
        return "排除", "比值或利润"
    parsed = parse_legacy_spread_name(name)
    if parsed is None:
        return "品种间套利", "其他"
    instruments, months = parsed.instruments, parsed.months
    if len(instruments) == 1 and len(months) == 2:
        if instruments[0] in SOY_INSTRUMENTS:
            return "豆系月差", "月差"
        if instruments[0] in PALM_RAPESEED_INSTRUMENTS:
            return "棕榈油与菜系月差", "月差"
        return "品种间套利", "其他"
    if len(instruments) == 2 and len(months) == 1:
        instrument_set = set(instruments)
        if instrument_set <= OIL_INSTRUMENTS:
            return "品种间套利", "油脂之间套利"
        if instrument_set <= MEAL_INSTRUMENTS:
            return "品种间套利", "粕之间套利"
        if instrument_set & OIL_INSTRUMENTS and instrument_set & MEAL_INSTRUMENTS:
            return "排除", "油粕跨类"
    return "品种间套利", "其他"


def display_spread_name(spread_name: str) -> str:
    parsed = parse_legacy_spread_name(spread_name)
    if parsed is None:
        return str(spread_name)
    labels = [INSTRUMENT_LABELS[instrument] for instrument in parsed.instruments]
    if len(parsed.instruments) == 1 and len(parsed.months) == 2:
        return f"{labels[0]} {parsed.months[0]:02d}-{parsed.months[1]:02d}"
    if len(parsed.instruments) == 2 and len(parsed.months) == 1:
        preferred_order = {"Y": 0, "OI": 1, "P": 2, "M": 0, "RM": 1}
        labels = [
            label
            for _, label in sorted(
                zip(parsed.instruments, labels, strict=True),
                key=lambda item: preferred_order[item[0]],
            )
        ]
        return f"{'-'.join(labels)} {parsed.months[0]:02d}"
    return str(spread_name)


def spread_sort_key(spread_name: str) -> tuple[int, int, int, str]:
    parsed = parse_legacy_spread_name(spread_name)
    if parsed is None:
        return (9, 9, 9, str(spread_name))
    instruments, months = parsed.instruments, parsed.months
    if len(instruments) == 1 and len(months) == 2:
        instrument_order = {"Y": 0, "M": 1, "P": 0, "OI": 1, "RM": 2}
        month_order = {(9, 1): 0, (1, 5): 1, (5, 9): 2}
        return (
            0,
            instrument_order.get(instruments[0], 9),
            month_order.get(months, 9),
            str(spread_name),
        )
    if len(instruments) == 2 and len(months) == 1:
        pair_order = {
            frozenset({"Y", "P"}): 0,
            frozenset({"Y", "OI"}): 1,
            frozenset({"OI", "P"}): 2,
            frozenset({"M", "RM"}): 3,
        }
        month_order = {1: 0, 5: 1, 9: 2}
        return (
            1,
            pair_order.get(frozenset(instruments), 9),
            month_order.get(months[0], 9),
            str(spread_name),
        )
    return (9, 9, 9, str(spread_name))


def configured_spreads(data: pd.DataFrame, config: pd.DataFrame) -> dict[str, list[str]]:
    available = set(data["spread_name"].dropna().astype(str))
    configured = config.get("spread_name", pd.Series(dtype=str)).dropna().astype(str).tolist()
    names = [name for name in configured if name in available]
    names.extend(sorted(available - set(names)))
    grouped = {board: [] for board in BOARD_OPTIONS}
    for name in names:
        board, _ = classify_board(name)
        if board in grouped:
            grouped[board].append(name)
    for board in grouped:
        grouped[board] = sorted(grouped[board], key=spread_sort_key)
    return grouped

"""Explicit shipment/hedge identities and full-CNF import profit models."""
from __future__ import annotations

from datetime import date
from types import MappingProxyType

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.soybean_margin.model import number, shipment_year
from agri_research_agent.soybean_margin.api_inputs import select_fx as soybean_select_fx

START = date(2026, 10, 10)
PROFILES = MappingProxyType({
    "canola": MappingProxyType(dict(label="加拿大菜籽", origin="Canada", tariff=.149,
        vat=.09, port_fee=50., processing_fee=220., meal_yield=.573, oil_yield=.417)),
    "palm": MappingProxyType(dict(label="24度精炼棕榈油", origin="Indonesia/Malaysia",
        tariff=.09, vat=.09, port_fee=80., processing_fee=0.)),
})


def hedge_period(year: int, month: int, *, short_transit=False) -> tuple[int, int]:
    if type(year) is not int or not 2000 <= year <= 2097 or type(month) is not int or not 1 <= month <= 12:
        raise ValueError("船期年月无效")
    if short_transit:
        return (year, 1) if month == 1 else ((year, 5) if month <= 5 else
               ((year, 9) if month <= 9 else (year + 1, 1)))
    return (year, 5) if month <= 3 else ((year, 9) if month <= 7 else
           ((year + 1, 1) if month <= 11 else (year + 1, 5)))


def contracts(day: date, commodity: str, month: int) -> tuple[ContractId, ...]:
    PROFILES[commodity]
    year, delivery = hedge_period(shipment_year(day, month), month, short_transit=commodity == "palm")
    exchange, products = (Exchange.DCE, ("P",)) if commodity == "palm" else (Exchange.CZCE, ("RM", "OI"))
    return tuple(ContractId(exchange, product, year, delivery) for product in products)


def symbol(contract: ContractId) -> str:
    if not 2000 <= contract.year <= 2099:
        raise ValueError("行情合约年份超出支持范围")
    return f"{contract.product}{contract.year % 100:02}{contract.month:02}"


def expected_contracts(day: date, commodity: str) -> dict[str, str]:
    return {symbol(c): str(c) for m in range(1, 13) for c in contracts(day, commodity, m)}


def select_fx(curve: dict, tenor: int) -> tuple:
    """Same policy as soybean: 1-2M spot, >=3M forward/interpolation."""
    if type(tenor) is not int or not 1 <= tenor <= 12:
        raise ValueError("汇率期限必须为1至12个月")
    rate, _, interpolated, lower, upper = soybean_select_fx(curve, tenor)
    return rate, interpolated, lower, upper


def calculate(commodity: str, cnf, fx, prices: tuple) -> dict:
    p = PROFILES[commodity]
    cnf, fx = number(cnf), number(fx)
    cost = (cnf * fx * (1+p["tariff"]) * (1+p["vat"])
            if cnf is not None and cnf >= 0 and fx is not None and fx > 0 else None)
    prices = tuple(number(v) for v in prices)
    if commodity == "palm":
        cost = cost + p["port_fee"] if cost is not None else None
    valid = len(prices) == (1 if commodity == "palm" else 2) and all(v is not None and v > 0 for v in prices)
    revenue = (prices[0] if commodity == "palm" else
               prices[0]*p["meal_yield"] + prices[1]*p["oil_yield"]) if valid else None
    profit = revenue - cost if revenue is not None and cost is not None else None
    if profit is not None and commodity == "canola":
        profit -= p["port_fee"] + p["processing_fee"]
    return dict(duty_paid_cost=cost, net_margin=profit)


def daily_rows(day: date, commodity: str, cnf: dict, snapshot: dict | None = None) -> list[dict]:
    if snapshot is not None and (snapshot["business_date"] != day.isoformat() or snapshot["commodity"] != commodity):
        raise ValueError("行情业务日期或品种不符")
    snapshot = snapshot or {}
    rows = []
    for month in range(1, 13):
        year = shipment_year(day, month)
        tenor = (year-day.year)*12 + month-day.month
        hedge = contracts(day, commodity, month)
        prices = tuple(snapshot.get("domestic", {}).get(symbol(c)) for c in hedge)
        fx, interpolated, lower, upper = select_fx(snapshot.get("fx_curve", {}), tenor)
        row = dict(business_date=day.isoformat(), commodity=commodity, shipment_year=year,
            shipment_month=month, shipment_period=f"{year}-{month:02}",
            domestic_contracts=[str(c) for c in hedge], cnf_usd_per_tonne=cnf.get(month),
            domestic_prices=prices, fx_value=fx, fx_tenor_months=tenor,
            fx_is_interpolated=interpolated, fx_lower_tenor=lower, fx_upper_tenor=upper)
        row.update(calculate(commodity, row["cnf_usd_per_tonne"], fx, prices))
        rows.append(row)
    return sorted(rows, key=lambda row: row["shipment_period"])

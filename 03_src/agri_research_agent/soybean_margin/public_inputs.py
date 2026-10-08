"""Read published FULL DAILY inputs; this consumer never contacts providers."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from agri_research_agent.data_sources.tankan.domestic_spread import full_contract_code
from agri_research_agent.pipelines.tankan_goal_a import load_current_files
from .model import ORIGINS, KEY, FIELDS, contracts, number


MARKET_COLUMNS = (
    "business_date", "exchange", "product", "contract_code", "price",
    "currency", "price_unit", "is_usable",
)
FX_COLUMNS = (
    "quote_date", "tenor_months", "rate", "base_currency", "quote_currency",
    "rate_unit", "is_usable",
)
DOMESTIC_COLUMNS = (
    "date", "status", "season",
    "leg1_instrument", "leg1_month", "leg1_price",
    "leg2_instrument", "leg2_month", "leg2_price",
)
MAX_VIEW_ROWS = 1_000_000
MAX_VIEW_BYTES = 64 * 1024 * 1024


def _read_view(path: Path, columns, predicate=None) -> pa.Table:
    dataset = ds.dataset(path, format="parquet")
    scanner = dataset.scanner(
        columns=list(columns), filter=predicate, batch_size=4096,
        batch_readahead=0, fragment_readahead=0, use_threads=False,
    )
    batches = []
    rows = size = 0
    for batch in scanner.to_batches():
        rows += batch.num_rows
        size += batch.nbytes
        if rows > MAX_VIEW_ROWS or size > MAX_VIEW_BYTES:
            raise ValueError("Public soybean input exceeds the bounded page read limit")
        batches.append(batch)
    return pa.Table.from_batches(batches, schema=scanner.projected_schema)


def read_public_tables(root: Path, domestic_path: Path):
    """Select business rows before pandas conversion, after whole-file authentication."""
    current = load_current_files(root / "public-market-data" / "tankan")
    if current is None:
        raise ValueError("Public market data is not published")
    market = _read_view(
        current.directory / "market.parquet", MARKET_COLUMNS,
        (ds.field("exchange") == "CBOT") & (ds.field("product") == "SOYBEAN"),
    )
    fx = _read_view(current.directory / "fx.parquet", FX_COLUMNS)
    names = pq.read_schema(domestic_path).names
    if not set(DOMESTIC_COLUMNS).issubset(names):
        raise ValueError("Domestic soybean input fields are incomplete")
    columns = [*DOMESTIC_COLUMNS,
               *(name for name in ("leg1_contract", "leg2_contract") if name in names)]
    domestic = _read_view(
        domestic_path, columns,
        (ds.field("status") == "success") & (
            ds.field("leg1_instrument").isin(["M", "Y"])
            | ds.field("leg2_instrument").isin(["M", "Y"])
        ),
    )
    return market.to_pandas(), fx.to_pandas(), domestic.to_pandas(), current.release_id


def _unique(rows, keys, value, label):
    if rows[keys].isna().any().any():
        raise ValueError(f"{label}业务键缺失")
    if rows.groupby(keys, dropna=False)[value].nunique(dropna=False).gt(1).any():
        raise ValueError(f"{label}同一业务键有不同数值")
    return rows.drop_duplicates(keys).set_index(keys)[value].to_dict()


def domestic_quotes(frame):
    """Recover exact contracts from the existing domestic-spread artifact."""
    rows = []
    for prefix in ("leg1", "leg2"):
        selected = frame.loc[frame.status.eq("success") & frame[f"{prefix}_instrument"].isin(["M", "Y"])]
        for row in selected.to_dict("records"):
            day = pd.Timestamp(row["date"]).date()
            code = row.get(f"{prefix}_contract")
            if not isinstance(code, str) or not code:
                code = full_contract_code(row[f"{prefix}_instrument"], row["season"], row[f"{prefix}_month"])
            rows.append(dict(business_date=day, contract=code,
                price=number(row[f"{prefix}_price"])))
    return pd.DataFrame(rows, columns=["business_date", "contract", "price"])


def _rate(curves, day, tenor):
    curve = curves.get(day, {})
    if tenor in curve:
        return curve[tenor], False, tenor, tenor
    lower = max((term for term in curve if term < tenor), default=None)
    upper = min((term for term in curve if term > tenor), default=None)
    if lower is None or upper is None:
        return None, False, lower, upper
    value = curve[lower] + (tenor-lower)/(upper-lower)*(curve[upper]-curve[lower])
    return value, True, lower, upper


def apply_public_inputs(history, market, fx, domestic, as_of: date):
    """Join exact date/contracts; never substitute yesterday or another contract."""
    market = market.loc[market.exchange.eq("CBOT") & market["product"].eq("SOYBEAN")].copy()
    if not market.empty and (not market.currency.eq("USD").all()
                            or not market.price_unit.eq("US_cents/bushel").all()):
        raise ValueError("CBOT币种或单位不符")
    fx = fx.copy()
    if not fx.empty and (not fx.base_currency.eq("USD").all()
                         or not fx.quote_currency.eq("CNH").all()
                         or not fx.rate_unit.eq("CNH_per_USD").all()):
        raise ValueError("汇率方向或单位不符")
    market["business_date"] = pd.to_datetime(market.business_date).dt.date
    fx["quote_date"] = pd.to_datetime(fx.quote_date).dt.date
    domestic = domestic_quotes(domestic)
    cbot = _unique(market.loc[market.is_usable.eq(True)],
        ["business_date", "contract_code"], "price", "CBOT")
    rates = _unique(fx.loc[fx.is_usable.eq(True)], ["quote_date", "tenor_months"], "rate", "汇率")
    curves = {}
    for (day, tenor), value in rates.items():
        value = number(value)
        if value is not None and value > 0:
            curves.setdefault(day, {})[tenor] = value
    dce = _unique(domestic, ["business_date", "contract"], "price", "国内盘面")
    earliest = min(history.business_date)
    dates = set(market.business_date) | set(fx.quote_date) | set(domestic.business_date) | {as_of}
    new_keys = []
    for day in sorted(dates):
        if day < earliest or day > as_of or day.weekday() >= 5:
            continue
        for origin in ORIGINS:
            for month in range(1, 13):
                _, _, year = contracts(day, month)
                new_keys.append(dict(business_date=day, origin=origin,
                    shipment_year=year, shipment_month=month))
    keys = pd.concat([history[KEY], pd.DataFrame(new_keys, columns=KEY)]).drop_duplicates(KEY)
    data = keys.merge(history, on=KEY, how="left", validate="one_to_one")
    mappings = [contracts(row.business_date, int(row.shipment_month)) for row in data.itertuples()]
    cbot_codes = [mapping[0] for mapping in mappings]
    domestic_codes = [mapping[1] for mapping in mappings]
    days = data.business_date.tolist()
    tenors = [(int(row.shipment_year)-row.business_date.year)*12
              + int(row.shipment_month)-row.business_date.month for row in data.itertuples()]
    data[FIELDS[1]] = [number(cbot.get((day, code))) for day,code in zip(days,cbot_codes)]
    selections = [_rate(curves, day, tenor) for day,tenor in zip(days,tenors)]
    data[FIELDS[2]] = [value[0] for value in selections]
    data["fx_is_interpolated"] = [value[1] for value in selections]
    data["fx_lower_tenor"] = [value[2] for value in selections]
    data["fx_upper_tenor"] = [value[3] for value in selections]
    data[FIELDS[3]] = [number(dce.get((day, "M"+code))) for day,code in zip(days,domestic_codes)]
    data[FIELDS[4]] = [number(dce.get((day, "Y"+code))) for day,code in zip(days,domestic_codes)]
    data["cbot_contract_year"] = [2000+int(code[:2]) for code in cbot_codes]
    data["cbot_contract_month"] = [int(code[2:]) for code in cbot_codes]
    data["soymeal_contract_code"] = ["M"+code for code in domestic_codes]
    data["soyoil_contract_code"] = ["Y"+code for code in domestic_codes]
    data["commodity"] = "soybean"
    latest = {"CBOT": max(market.business_date, default=None),
              "汇率": max(fx.quote_date, default=None),
              "国内盘面": max(domestic.business_date, default=None)}
    return data.drop(columns=["retained_net_margin"], errors="ignore"), latest

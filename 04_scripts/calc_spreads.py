from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


PRODUCT_NAMES = {
    "Y": "豆油",
    "P": "棕榈油",
    "OI": "菜油",
    "M": "豆粕",
    "RM": "菜粕",
}

PRODUCT_PREFIXES = sorted(PRODUCT_NAMES, key=len, reverse=True)
DEFAULT_PRICE_FIELDS = ["current_price", "close", "settle", "last_settle_price"]
LEG_COUNT = 4


def safe_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def safe_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number) or number == 0:
        return None
    return number


def enabled(value: object) -> bool:
    text = safe_text(value).lower()
    if text in {"", "1", "true", "yes", "y", "是", "启用"}:
        return True
    return False


def split_symbol(symbol: str) -> tuple[str, str]:
    upper_symbol = symbol.upper()
    for prefix in PRODUCT_PREFIXES:
        if upper_symbol.startswith(prefix):
            return prefix, upper_symbol[len(prefix) :]
    return upper_symbol, ""


def display_name_for_symbol(symbol: str) -> str:
    prefix, suffix = split_symbol(symbol)
    product_name = PRODUCT_NAMES.get(prefix, prefix)
    if suffix in {"", "0"}:
        return f"{product_name}连续"
    return f"{product_name}{suffix}"


def alternate_display_names(symbol: str) -> list[str]:
    prefix, suffix = split_symbol(symbol)
    names = [display_name_for_symbol(symbol)]
    product_name = PRODUCT_NAMES.get(prefix, prefix)
    if len(suffix) == 4 and suffix.startswith("2"):
        names.append(f"{product_name}{suffix[1:]}")
    if len(suffix) == 3:
        names.append(f"{product_name}2{suffix}")
    return list(dict.fromkeys(names))


def append_log(log_file: Path, message: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("calc_spreads")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def read_config(config_file: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    spreads = pd.read_excel(config_file, sheet_name="spreads")
    settings: dict[str, str] = {}
    try:
        settings_df = pd.read_excel(config_file, sheet_name="settings")
        for _, row in settings_df.iterrows():
            key = safe_text(row.get("key"))
            if key:
                settings[key] = safe_text(row.get("value"))
    except ValueError:
        pass

    required = {"enabled", "spread_name", "leg1_symbol", "leg1_multiplier", "leg2_symbol", "leg2_multiplier"}
    missing = sorted(required - set(spreads.columns))
    if missing:
        raise ValueError(f"Missing required config columns: {', '.join(missing)}")

    return spreads, settings


def collect_symbols(spreads: pd.DataFrame) -> list[str]:
    symbols: list[str] = []
    for _, row in spreads.iterrows():
        if not enabled(row.get("enabled")):
            continue
        for index in range(1, LEG_COUNT + 1):
            symbol = safe_text(row.get(f"leg{index}_symbol")).upper()
            multiplier = safe_number(row.get(f"leg{index}_multiplier"))
            if symbol and multiplier is not None and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def fetch_spot_prices(symbols: list[str], market: str, adjust: str, logger: logging.Logger) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    batch_symbol = ",".join(symbols)
    try:
        data = ak.futures_zh_spot(symbol=batch_symbol, market=market, adjust=adjust)
        if not isinstance(data, pd.DataFrame):
            raise TypeError("futures_zh_spot did not return a DataFrame")
        logger.info("spot fetch succeeded: requested=%s rows=%s", batch_symbol, len(data))
        return data.copy()
    except Exception as exc:  # noqa: BLE001
        logger.exception("spot fetch failed: requested=%s error=%s", batch_symbol, exc)
        return pd.DataFrame()


def price_from_row(row: pd.Series, preferred_field: str) -> tuple[float | None, str]:
    fields = [preferred_field] if preferred_field else []
    fields.extend(field for field in DEFAULT_PRICE_FIELDS if field not in fields)
    for field in fields:
        if field in row.index:
            number = safe_number(row.get(field))
            if number is not None:
                return number, field
    return None, ""


def spot_price_for_symbol(
    symbol: str,
    spot_df: pd.DataFrame,
    preferred_field: str,
) -> tuple[float | None, str, dict[str, Any] | None]:
    if spot_df.empty or "symbol" not in spot_df.columns:
        return None, "", None

    candidates = [symbol, *alternate_display_names(symbol)]
    match = spot_df[spot_df["symbol"].astype(str).isin(candidates)]
    if match.empty:
        return None, "", None

    row = match.iloc[0]
    price, field = price_from_row(row, preferred_field)
    if price is None:
        return None, "", row.to_dict()
    return price, field, row.to_dict()


def daily_price_for_symbol(
    symbol: str,
    preferred_field: str,
    logger: logging.Logger,
) -> tuple[float | None, str, dict[str, Any] | None, str]:
    try:
        data = ak.futures_zh_daily_sina(symbol=symbol)
        if not isinstance(data, pd.DataFrame) or data.empty:
            return None, "", None, "daily returned no rows"
        row = data.iloc[-1]
        price, field = price_from_row(row, preferred_field)
        if price is None:
            return None, "", row.to_dict(), "daily row has no usable price"
        logger.info("daily fetch succeeded: symbol=%s rows=%s", symbol, len(data))
        return price, field, row.to_dict(), ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("daily fetch failed: symbol=%s error=%s", symbol, exc)
        return None, "", None, f"{type(exc).__name__}: {exc}"


def get_leg_price(
    symbol: str,
    spot_df: pd.DataFrame,
    preferred_field: str,
    source_preference: str,
    logger: logging.Logger,
) -> tuple[dict[str, Any] | None, str]:
    sources = {
        "spot": ["spot"],
        "daily": ["daily"],
        "daily_then_spot": ["daily", "spot"],
        "spot_then_daily": ["spot", "daily"],
    }.get(source_preference, ["spot", "daily"])

    errors: list[str] = []
    for source in sources:
        if source == "spot":
            price, field, raw_row = spot_price_for_symbol(symbol, spot_df, preferred_field)
            if price is not None:
                return (
                    {
                        "symbol": symbol,
                        "display_symbol": display_name_for_symbol(symbol),
                        "source": "futures_zh_spot",
                        "price_field": field,
                        "price": price,
                        "raw": raw_row,
                    },
                    "",
                )
            errors.append("spot not matched or no usable price")
        else:
            price, field, raw_row, error = daily_price_for_symbol(symbol, preferred_field, logger)
            if price is not None:
                return (
                    {
                        "symbol": symbol,
                        "display_symbol": display_name_for_symbol(symbol),
                        "source": "futures_zh_daily_sina",
                        "price_field": field,
                        "price": price,
                        "raw": raw_row,
                    },
                    "",
                )
            errors.append(error or "daily not matched or no usable price")

    return None, "; ".join(errors)


def calculate_spreads(
    spreads: pd.DataFrame,
    spot_df: pd.DataFrame,
    default_price_field: str,
    default_source_preference: str,
    logger: logging.Logger,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    result_rows: list[dict[str, Any]] = []
    leg_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    run_time = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for config_index, row in spreads.iterrows():
        if not enabled(row.get("enabled")):
            continue

        spread_name = safe_text(row.get("spread_name")) or f"spread_{config_index + 1}"
        price_field = safe_text(row.get("price_field")) or default_price_field
        source_preference = safe_text(row.get("source_preference")) or default_source_preference

        spread_value = 0.0
        spread_formula_parts: list[str] = []
        ok = True

        for leg_index in range(1, LEG_COUNT + 1):
            symbol = safe_text(row.get(f"leg{leg_index}_symbol")).upper()
            multiplier = safe_number(row.get(f"leg{leg_index}_multiplier"))
            if not symbol and multiplier is None:
                continue
            if not symbol or multiplier is None:
                ok = False
                failure_rows.append(
                    {
                        "spread_name": spread_name,
                        "leg": leg_index,
                        "symbol": symbol,
                        "error": "symbol or multiplier missing",
                    }
                )
                continue

            leg_price, error = get_leg_price(
                symbol=symbol,
                spot_df=spot_df,
                preferred_field=price_field,
                source_preference=source_preference,
                logger=logger,
            )
            if leg_price is None:
                ok = False
                failure_rows.append(
                    {
                        "spread_name": spread_name,
                        "leg": leg_index,
                        "symbol": symbol,
                        "error": error,
                    }
                )
                continue

            contribution = multiplier * float(leg_price["price"])
            spread_value += contribution
            spread_formula_parts.append(f"{multiplier:g}*{symbol}")
            leg_rows.append(
                {
                    "run_time": run_time,
                    "spread_name": spread_name,
                    "leg": leg_index,
                    "symbol": symbol,
                    "display_symbol": leg_price["display_symbol"],
                    "multiplier": multiplier,
                    "price": leg_price["price"],
                    "contribution": contribution,
                    "source": leg_price["source"],
                    "price_field": leg_price["price_field"],
                }
            )

        result_rows.append(
            {
                "run_time": run_time,
                "spread_name": spread_name,
                "status": "success" if ok else "failed",
                "spread_value": spread_value if ok else None,
                "formula": " + ".join(spread_formula_parts),
                "notes": safe_text(row.get("notes")),
            }
        )

    return pd.DataFrame(result_rows), pd.DataFrame(leg_rows), pd.DataFrame(failure_rows)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    config_file = project_root / "01_data" / "spread_config.xlsx"
    output_dir = project_root / "06_outputs"
    logs_dir = project_root / "10_logs"
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M")

    output_file = output_dir / f"spreads_{timestamp}.xlsx"
    log_file = logs_dir / f"spreads_{timestamp}.log"

    if output_file.exists() or log_file.exists():
        print(f"Output or log already exists for timestamp {timestamp}; will not overwrite.")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(log_file)
    append_log(log_file, "Spread calculation started.")

    try:
        spreads, settings = read_config(config_file)
        default_market = settings.get("spot_market", "CF")
        default_adjust = settings.get("spot_adjust", "0")
        default_price_field = settings.get("price_field", "current_price")
        default_source_preference = settings.get("source_preference", "spot_then_daily")

        symbols = collect_symbols(spreads)
        spot_df = fetch_spot_prices(symbols, default_market, default_adjust, logger)
        results_df, legs_df, failures_df = calculate_spreads(
            spreads=spreads,
            spot_df=spot_df,
            default_price_field=default_price_field,
            default_source_preference=default_source_preference,
            logger=logger,
        )

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            results_df.to_excel(writer, sheet_name="spread_results", index=False)
            legs_df.to_excel(writer, sheet_name="leg_prices", index=False)
            failures_df.to_excel(writer, sheet_name="failures", index=False)
            spot_df.to_excel(writer, sheet_name="raw_spot", index=False)
            spreads.to_excel(writer, sheet_name="config_used", index=False)

        append_log(log_file, f"Spread calculation finished. Output: {output_file}")
        print(f"Spread report generated: {output_file}")
        print(f"Spread log generated: {log_file}")
        print(f"Success rows: {(results_df['status'] == 'success').sum() if not results_df.empty else 0}")
        print(f"Failed rows: {(results_df['status'] == 'failed').sum() if not results_df.empty else 0}")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Spread calculation failed: %s", exc)
        append_log(log_file, f"Spread calculation failed: {type(exc).__name__}: {exc}")
        print(f"Spread calculation failed. See log: {log_file}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


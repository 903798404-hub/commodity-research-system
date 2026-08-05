"""Stream ``basis_price`` from the Navicat SQL dump into the stable basis schema."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from agri_research_agent.data_sources.basis_database import DATABASE_COLUMNS


BASIS_TABLE = "basis_price"
SOURCE_QUOTE_TYPE = "现货基差"
OUTPUT_QUOTE_TYPE = "基差报价"
OUTPUT_DELIVERY_MONTH = "现货"
PRODUCT_MAP = {
    "大豆油": "一豆",
    "菜籽油": "三菜",
    "棕榈油": "24度",
    "豆粕": "豆粕",
    "菜粕": "菜粕",
}
EXPECTED_SOURCE_COLUMNS = [
    "日期",
    "品种",
    "文章ID",
    "文章标题",
    "行类型",
    "地区",
    "省份",
    "工厂",
    "合同情况",
    "基差合同年",
    "基差合同月",
    "价格原始",
    "期货合约",
    "基差",
    "现货价",
    "成交量",
    "created_at",
    "报价类别",
    "期货收盘价",
]
STABLE_KEY = [
    "date",
    "commodity",
    "region",
    "quote_type",
    "delivery_month",
    "futures_contract",
]
PAGE_KEY = ["date", "commodity", "region", "quote_type", "delivery_month"]
NEAR_CONTRACT_GROUP_KEY = ["date", "commodity", "region", "quote_type"]
_CREATE_RE = re.compile(r"^CREATE\s+TABLE\s+`basis_price`", re.IGNORECASE)
_COLUMN_RE = re.compile(r"^\s*`([^`]+)`\s+(.+?)(?:,)?$")
_DML_RE = re.compile(
    r"^(INSERT\s+INTO|REPLACE\s+INTO|UPDATE)\s+`basis_price`",
    re.IGNORECASE,
)
_CONTRACT_RE = re.compile(r"^(\d{2})(0[1-9]|1[0-2])$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_values(statement: str) -> list[str | None]:
    values_at = statement.upper().find("VALUES")
    position = statement.find("(", values_at) + 1
    if values_at < 0 or position <= 0:
        raise ValueError("basis_price DML 缺少 VALUES")
    values: list[str | None] = []
    length = len(statement)
    while position < length:
        while position < length and statement[position].isspace():
            position += 1
        if position >= length or statement[position] == ")":
            break
        if statement[position] == "'":
            position += 1
            text: list[str] = []
            while position < length:
                character = statement[position]
                if character == "\\":
                    position += 1
                    if position >= length:
                        raise ValueError("basis_price 字符串转义不完整")
                    escaped = statement[position]
                    text.append(
                        {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a"}.get(
                            escaped, escaped
                        )
                    )
                    position += 1
                elif character == "'":
                    if position + 1 < length and statement[position + 1] == "'":
                        text.append("'")
                        position += 2
                    else:
                        position += 1
                        break
                else:
                    text.append(character)
                    position += 1
            values.append("".join(text))
        else:
            end = position
            while end < length and statement[end] not in ",)":
                end += 1
            token = statement[position:end].strip()
            values.append(None if token.upper() == "NULL" else token)
            position = end
        while position < length and statement[position].isspace():
            position += 1
        if position < length and statement[position] == ",":
            position += 1
        elif position < length and statement[position] == ")":
            break
    return values


def _parse_contract(value: str | None) -> tuple[int, int] | None:
    if value is None or not str(value).strip():
        return None
    match = _CONTRACT_RE.fullmatch(str(value).strip())
    if not match:
        return None
    return 2000 + int(match.group(1)), int(match.group(2))


def _empty_database() -> pd.DataFrame:
    database = pd.DataFrame(columns=DATABASE_COLUMNS)
    database["date"] = pd.to_datetime(database["date"])
    for column in (
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "futures_contract",
        "source_sheet",
    ):
        database[column] = database[column].astype("string")
    for column in ("cash_price", "futures_price", "basis"):
        database[column] = database[column].astype("float64")
    return database


def build_basis_candidate(
    source_path: Path | str,
    output_path: Path | str,
    *,
    no_valid_group_limit: float = 0.01,
    minimum_latest_date: str = "2026-08-04",
) -> dict[str, Any]:
    """Build one deterministic candidate without loading the SQL dump at once."""
    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"国内基差 SQL 不存在：{source}")

    source_sha_before = sha256_file(source)
    source_columns: list[str] = []
    source_types: dict[str, str] = {}
    in_create = False
    operations: Counter[str] = Counter()
    raw_rows = 0
    spot_basis_rows = 0
    null_basis_rows = 0
    valid_basis_rows = 0
    raw_product_counts: Counter[str] = Counter()
    missing_contract_rows = 0
    invalid_contract_rows = 0
    expired_contract_rows = 0
    zero_basis_rows = 0
    raw_delivery_month_counts: Counter[str] = Counter()
    groups: dict[tuple[dt.date, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "contracts": defaultdict(list),
            "raw_contracts": set(),
            "raw_delivery_months": Counter(),
            "original": "",
        }
    )

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            statement = line.rstrip("\r\n")
            if _CREATE_RE.match(statement):
                in_create = True
                continue
            if in_create:
                column = _COLUMN_RE.match(statement)
                if column:
                    source_columns.append(column.group(1))
                    source_types[column.group(1)] = column.group(2).rstrip(",")
                if re.match(r"^\)\s+ENGINE", statement, re.IGNORECASE):
                    in_create = False
                    if source_columns != EXPECTED_SOURCE_COLUMNS:
                        raise ValueError(
                            "basis_price 字段与受支持结构不一致："
                            f"实际={source_columns}"
                        )
                continue

            dml = _DML_RE.match(statement)
            if not dml:
                continue
            operation = dml.group(1).upper().replace("  ", " ")
            operations[operation] += 1
            if operation == "UPDATE":
                raise ValueError(
                    f"第 {line_number} 行包含 basis_price UPDATE，无法安全流式还原"
                )
            values = _parse_values(statement)
            if len(values) != len(source_columns):
                raise ValueError(
                    f"第 {line_number} 行字段数为 {len(values)}，"
                    f"CREATE TABLE 字段数为 {len(source_columns)}"
                )
            raw_rows += 1
            row = dict(zip(source_columns, values, strict=True))
            if row["报价类别"] != SOURCE_QUOTE_TYPE:
                continue
            spot_basis_rows += 1
            if row["基差"] is None:
                null_basis_rows += 1
                continue
            try:
                business_date = dt.date.fromisoformat(str(row["日期"]))
            except ValueError as exc:
                raise ValueError(
                    f"第 {line_number} 行业务日期无法解析：{row['日期']}"
                ) from exc
            original_product = str(row["品种"])
            if original_product not in PRODUCT_MAP:
                raise ValueError(f"第 {line_number} 行出现未知品种：{original_product}")
            region = str(row["地区"] or "").strip()
            raw_delivery_month = str(row["基差合同月"] or "").strip() or "<EMPTY>"
            if not region:
                raise ValueError(f"第 {line_number} 行缺少地区")
            try:
                basis = float(str(row["基差"]))
            except ValueError as exc:
                raise ValueError(
                    f"第 {line_number} 行基差不是有效数值：{row['基差']}"
                ) from exc
            valid_basis_rows += 1
            zero_basis_rows += int(basis == 0)
            raw_product_counts[original_product] += 1
            raw_delivery_month_counts[raw_delivery_month] += 1
            key = (
                business_date,
                PRODUCT_MAP[original_product],
                region,
                OUTPUT_QUOTE_TYPE,
            )
            group = groups[key]
            group["original"] = original_product
            group["raw_delivery_months"][raw_delivery_month] += 1
            raw_contract = None if row["期货合约"] is None else str(row["期货合约"]).strip()
            group["raw_contracts"].add(raw_contract)
            if not raw_contract:
                missing_contract_rows += 1
                continue
            parsed_contract = _parse_contract(raw_contract)
            if parsed_contract is None:
                invalid_contract_rows += 1
                continue
            if parsed_contract < (business_date.year, business_date.month):
                expired_contract_rows += 1
                continue
            group["contracts"][parsed_contract].append(
                {
                    "basis": basis,
                    "contract": raw_contract,
                    "article_id": row["文章ID"],
                    "factory": row["工厂"],
                    "raw_delivery_month": raw_delivery_month,
                }
            )

    if not source_columns:
        raise ValueError("未发现 CREATE TABLE `basis_price`")
    if not operations:
        raise ValueError("未发现 basis_price INSERT 或 REPLACE")

    rows: list[dict[str, Any]] = []
    no_valid_groups: list[dict[str, Any]] = []
    far_contract_excluded_rows = 0
    selected_factory_rows = 0
    aggregate_product_counts: Counter[str] = Counter()
    merged_delivery_month_groups: list[dict[str, Any]] = []
    for key in sorted(groups):
        business_date, commodity, region, quote_type = key
        group = groups[key]
        if not group["contracts"]:
            no_valid_groups.append(
                {
                    "date": business_date.isoformat(),
                    "commodity": commodity,
                    "region": region,
                    "quote_type": quote_type,
                    "raw_delivery_months": dict(
                        sorted(group["raw_delivery_months"].items())
                    ),
                    "raw_contracts": sorted(
                        "<NULL>" if value is None else value
                        for value in group["raw_contracts"]
                    ),
                }
            )
            continue
        target_contract = min(group["contracts"])
        target_records = group["contracts"][target_contract]
        if len(group["raw_delivery_months"]) > 1:
            merged_delivery_month_groups.append(
                {
                    "date": business_date.isoformat(),
                    "commodity": commodity,
                    "region": region,
                    "quote_type": quote_type,
                    "raw_delivery_months": dict(
                        sorted(group["raw_delivery_months"].items())
                    ),
                    "target_contract": target_records[0]["contract"],
                }
            )
        far_contract_excluded_rows += sum(
            len(records)
            for contract, records in group["contracts"].items()
            if contract != target_contract
        )
        selected_factory_rows += len(target_records)
        basis_median = float(
            statistics.median(record["basis"] for record in target_records)
        )
        contract_code = target_records[0]["contract"]
        rows.append(
            {
                "date": pd.Timestamp(business_date),
                "commodity": commodity,
                "region": region,
                "quote_type": quote_type,
                "delivery_month": OUTPUT_DELIVERY_MONTH,
                "futures_contract": contract_code,
                "cash_price": float("nan"),
                "futures_price": float("nan"),
                "basis": basis_median,
                "source_sheet": f"basis_price:{group['original']}",
            }
        )
        aggregate_product_counts[commodity] += 1

    total_groups = len(groups)
    no_valid_ratio = len(no_valid_groups) / total_groups if total_groups else 1.0
    if no_valid_ratio > no_valid_group_limit:
        raise ValueError(
            "无有效未到期合约的聚合组超过质量门："
            f"{len(no_valid_groups)}/{total_groups}={no_valid_ratio:.2%}"
        )
    database = _empty_database() if not rows else pd.DataFrame(rows, columns=DATABASE_COLUMNS)
    database["date"] = pd.to_datetime(database["date"], errors="raise").astype(
        "datetime64[us]"
    )
    for column in (
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "futures_contract",
        "source_sheet",
    ):
        database[column] = database[column].astype("string")
    for column in ("cash_price", "futures_price", "basis"):
        database[column] = pd.to_numeric(database[column], errors="raise").astype("float64")
    database = database.sort_values(STABLE_KEY, kind="mergesort").reset_index(drop=True)
    if database.empty:
        raise ValueError("近月规则筛选后没有候选记录")
    if set(database["delivery_month"].astype(str)) != {OUTPUT_DELIVERY_MONTH}:
        raise ValueError("候选 delivery_month 必须全部为现货")
    duplicate_count = int(database.duplicated(STABLE_KEY, keep=False).sum())
    if duplicate_count:
        raise ValueError(f"候选稳定键存在 {duplicate_count} 条重复记录")
    page_contracts = database.groupby(PAGE_KEY, dropna=False)["futures_contract"].nunique()
    if (page_contracts > 1).any():
        raise ValueError("近月选择后页面粒度仍包含多个期货合约")
    actual_products = set(database["commodity"].astype(str))
    expected_products = set(PRODUCT_MAP.values())
    if actual_products != expected_products:
        raise ValueError(
            f"候选品种不完整：实际={sorted(actual_products)}，预期={sorted(expected_products)}"
        )
    latest_date = database["date"].max()
    latest_source_products = {
        key[1] for key in groups if key[0] == latest_date.date()
    }
    latest_candidate_products = set(
        database.loc[database["date"].eq(latest_date), "commodity"].astype(str)
    )
    missing_latest = sorted(latest_source_products - latest_candidate_products)
    if missing_latest:
        raise ValueError(
            "最新业务日期已有报价品种被合约规则全部删除："
            + "、".join(missing_latest)
        )
    if latest_candidate_products != expected_products:
        raise ValueError(
            "最新业务日期未覆盖全部正式品种："
            f"实际={sorted(latest_candidate_products)}"
        )
    minimum_latest = pd.Timestamp(minimum_latest_date)
    if latest_date < minimum_latest:
        raise ValueError(
            "候选最大业务日期早于允许下限："
            f"实际={latest_date:%Y-%m-%d}，下限={minimum_latest:%Y-%m-%d}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        database.to_parquet(temporary, index=False)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    source_sha_after = sha256_file(source)
    if source_sha_after != source_sha_before:
        output.unlink(missing_ok=True)
        raise ValueError("SQL 在读取过程中发生变化，候选已拒绝")

    return {
        "source_file": source,
        "output_file": output,
        "source_sha256_before": source_sha_before,
        "source_sha256_after": source_sha_after,
        "source_columns": source_columns,
        "source_types": source_types,
        "operations": dict(operations),
        "raw_rows": raw_rows,
        "spot_basis_rows": spot_basis_rows,
        "null_basis_rows": null_basis_rows,
        "valid_basis_rows": valid_basis_rows,
        "raw_product_counts": dict(raw_product_counts),
        "raw_delivery_month_counts": dict(sorted(raw_delivery_month_counts.items())),
        "merged_delivery_month_groups": merged_delivery_month_groups,
        "missing_contract_rows": missing_contract_rows,
        "invalid_contract_rows": invalid_contract_rows,
        "expired_contract_rows": expired_contract_rows,
        "far_contract_excluded_rows": far_contract_excluded_rows,
        "selected_factory_rows": selected_factory_rows,
        "zero_basis_rows": zero_basis_rows,
        "total_groups": total_groups,
        "no_valid_groups": no_valid_groups,
        "no_valid_group_ratio": no_valid_ratio,
        "rows": len(database),
        "aggregate_product_counts": dict(aggregate_product_counts),
        "earliest_date": database["date"].min(),
        "latest_date": latest_date,
        "stable_key": STABLE_KEY,
        "near_contract_group_key": NEAR_CONTRACT_GROUP_KEY,
    }

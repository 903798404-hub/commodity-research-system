"""Read-only roots and explicit collection plans for independent domains."""
from __future__ import annotations

from datetime import date, timedelta
import os
from pathlib import Path
import re
import sys

from agri_research_agent.oilseed_positions.model import config, preview_root as oilseed_root
from agri_research_agent.sugar_positions.model import MARKETS
from agri_research_agent.sugar_positions.storage import preview_root as sugar_root

DOMAINS = {"sugar": "白糖", "rapeseed": "菜籽", "soybean": "大豆", "palm": "棕榈油"}
STABLE_RELATIVE_ROOT = Path("processed/commodity_positions")


def local_root(project_root: Path, domain: str) -> Path:
    if domain not in DOMAINS:
        raise ValueError("持仓板块无效")
    return sugar_root(project_root) if domain == "sugar" else oilseed_root(project_root, domain)


def data_root(project_root: Path, domain: str) -> Path:
    """An explicit runtime root never falls back to local preview data."""
    if domain not in DOMAINS:
        raise ValueError("持仓板块无效")
    configured = os.getenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", "").strip()
    formal = Path(configured or project_root / "01_data") / STABLE_RELATIVE_ROOT / domain
    if configured or (formal / "current.json").exists() or not (project_root / ".git").is_file():
        return formal
    return local_root(project_root, domain)


def validate_domain(snapshot: dict, project_root: Path, domain: str) -> dict:
    if domain == "sugar":
        foreign, domestic = set(MARKETS), {"SR"}
    else:
        spec = config(project_root)[domain]
        foreign, domestic = set(spec["foreign"]), set(spec["domestic"])
    if any(row["market"] not in foreign for row in snapshot["foreign"]):
        raise ValueError("快照外盘市场与所选板块不符")
    if any(re.sub(r"\d+$", "", row["scope"]) not in domestic for row in snapshot["domestic"]):
        raise ValueError("快照国内品种与所选板块不符")
    return snapshot


def collection_plan(project_root: Path, domains: list[str], markets: str, end: date, days: int,
                    start_year: int, *, recheck: bool = False) -> list[dict]:
    if not domains or len(domains) != len(set(domains)) or set(domains) - set(DOMAINS):
        raise ValueError("持仓板块必须有效且不重复")
    if markets not in {"all", "domestic", "foreign"} or not 1 <= days <= 120:
        raise ValueError("采集口径或日期范围无效")
    if not 2006 <= start_year <= end.year or end - timedelta(days=days - 1) < date(2025, 11, 2):
        raise ValueError("采集年份或国内日期范围无效")
    start = (end - timedelta(days=days - 1)).isoformat()
    specs = config(project_root)
    plan = []
    for domain in domains:
        args = [sys.executable]
        if domain == "sugar":
            args += [str(project_root / "04_scripts/sugar_positions/update_sugar_positions.py"),
                     "--czce-start", start, "--czce-end", end.isoformat()]
        else:
            args += [str(project_root / "04_scripts/oilseed_positions/update_positions.py"),
                     "--domain", domain, "--domestic-start", start, "--end", end.isoformat()]
            # Recheck recent weekly reports without allowing the initial
            # onboarding date to expand into an unbounded schedule request.
            euro_start = max(date(2026, 9, 30), end - timedelta(days=35))
            args += ["--euronext-start", min(euro_start, end).isoformat()]
            contracts = [scope for variety, scope in specs[domain].get("default_scopes", {}).items()
                         if scope != variety]
            if contracts and markets != "foreign":
                args += ["--sina-contracts", *contracts]
        args += ["--markets", markets, "--start-year", str(start_year)]
        if recheck:
            args.append("--recheck-domestic")
        plan.append({"domain": domain, "argv": args})
    return plan

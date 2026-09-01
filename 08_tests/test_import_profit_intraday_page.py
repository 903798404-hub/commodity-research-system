from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from agri_research_agent.import_profit.cnf_store import (
    CnfQuoteUpdate,
    load_cnf_store,
    upsert_cnf_quotes,
)
from agri_research_agent.import_profit.intraday_store import (
    SoybeanIntradayResultBatch,
    seal_intraday_profit_batch,
)
from agri_research_agent.import_profit.intraday import (
    calculate_soybean_intraday_profit,
    select_soybean_intraday_market_inputs,
)
from agri_research_agent.market_data.intraday import MarketSession
from agri_research_agent.market_data.intraday import seal_intraday_snapshot
from test_import_profit_intraday import CONFIG, DAY, calculated, key, mixed_snapshot


def _assets(tmp_path: Path) -> tuple[Path, Path]:
    cnf_path = tmp_path / "cnf.parquet"
    business_key = key()
    upsert_cnf_quotes(
        cnf_path,
        (
            CnfQuoteUpdate(
                business_key,
                150.0,
                "manual_ui",
                datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
                "page-fixture",
            ),
        ),
        allowed_origins=CONFIG.origin_codes,
        expected_store_sha256=None,
    )
    cnf_identity = load_cnf_store(
        cnf_path, allowed_origins=CONFIG.origin_codes
    ).store_sha256
    assert cnf_identity is not None
    result_root = tmp_path / "results"
    for session, delta in ((MarketSession.AM, 0), (MarketSession.PM, 10)):
        result = calculated(
            session,
            business_key,
            delta=delta,
            cnf_identity=cnf_identity,
        )
        seal_intraday_profit_batch(
            result_root,
            SoybeanIntradayResultBatch(
                DAY,
                session,
                result.market_snapshot_release_id,
                result.market_snapshot_sha256,
                result.market_captured_at,
                result.cnf_identity,
                result.calculated_at,
                (result,),
            ),
        )
    return result_root, cnf_path


def _app_script(
    result_root: Path, cnf_path: Path, snapshot_root: Path | None = None
) -> str:
    return f"""
from pathlib import Path
from agri_research_agent.import_profit.config import load_soybean_config
from import_profit_intraday_page import IntradayPageDataPaths, render_import_profit_intraday_page
config = load_soybean_config({str(ROOT / '02_configs/import_profit_soybean.yaml')!r})
render_import_profit_intraday_page(
    IntradayPageDataPaths(
        Path({str(result_root)!r}),
        Path({str(cnf_path)!r}),
        {f'Path({str(snapshot_root)!r})' if snapshot_root is not None else 'None'},
    ),
    config=config,
)
"""


def test_page_distinguishes_sealed_am_waiting_for_cnf_from_missing_pm(
    tmp_path: Path,
) -> None:
    result_root, cnf_path = _assets(tmp_path)
    empty_results = tmp_path / "empty-results"
    empty_results.mkdir()
    snapshot_root = tmp_path / "snapshots"
    snapshot = mixed_snapshot(
        MarketSession.AM, key(year=2026, month=12), key(year=2027, month=8)
    )
    seal_intraday_snapshot(snapshot_root, snapshot)

    app = AppTest.from_string(
        _app_script(empty_results, cnf_path, snapshot_root), default_timeout=20
    ).run(timeout=20)

    assert not app.exception
    status = "\n".join(item.value for item in app.markdown)
    assert "AM：尚未封存" in status
    assert "PM：尚未封存" in status


def test_page_has_only_required_am_pm_cnf_and_profit_chart(tmp_path: Path) -> None:
    result_root, cnf_path = _assets(tmp_path)
    app = AppTest.from_string(
        _app_script(result_root, cnf_path), default_timeout=20
    ).run(timeout=20)
    assert not app.exception
    html = "\n".join(item.proto.body for item in app.get("html"))
    assert "soy-title" in html
    assert "中国进口大豆盘面榨利" in html
    assert "大豆早间榨利" in html
    assert "大豆下午榨利" in html
    assert "soy-profit-table" in html
    assert "关税%" in html and "3%" in html
    assert "增值税%" in html and "9%" in html
    assert "粕价值" not in html and "油价值" not in html
    assert not app.selectbox
    assert len(app.dataframe) == 1  # the accepted CNF editor only


def test_page_module_has_no_database_or_producer_boundary() -> None:
    source = (ROOT / "05_apps/import_profit_intraday_page.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "TankanClient",
        "capture_public_intraday",
        "materialize_soybean_intraday",
        "data_sources.tankan",
        "refresh",
    )
    assert all(token not in source for token in forbidden)
    assert "CNF 折线图" not in source
    assert "完税成本折线图" not in source


def test_page_renders_unavailable_row_and_history_ignores_it(tmp_path: Path) -> None:
    available_key = key(year=2026, month=12)
    unavailable_key = key(year=2027, month=9)
    cnf_path = tmp_path / "cnf.parquet"
    upsert_cnf_quotes(
        cnf_path,
        tuple(
            CnfQuoteUpdate(
                business_key,
                value,
                "manual_ui",
                datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
                "page-partial-fixture",
            )
            for business_key, value in (
                (available_key, 150.0),
                (unavailable_key, 151.0),
            )
        ),
        allowed_origins=CONFIG.origin_codes,
        expected_store_sha256=None,
    )
    cnf = load_cnf_store(cnf_path, allowed_origins=CONFIG.origin_codes)
    assert cnf.store_sha256 is not None
    result_root = tmp_path / "results"
    for session in (MarketSession.AM, MarketSession.PM):
        snapshot = mixed_snapshot(session, available_key, unavailable_key)
        results = tuple(
            calculate_soybean_intraday_profit(
                select_soybean_intraday_market_inputs(
                    snapshot, business_key=business_key, config=CONFIG
                ),
                cnf_store=cnf,
                config=CONFIG,
                calculated_at=datetime(2026, 8, 28, 7, 30, tzinfo=timezone.utc),
            )
            for business_key in (available_key, unavailable_key)
        )
        seal_intraday_profit_batch(
            result_root,
            SoybeanIntradayResultBatch(
                DAY,
                session,
                snapshot.release_id,
                snapshot.content_sha256,
                snapshot.captured_at,
                cnf.store_sha256,
                datetime(2026, 8, 28, 7, 30, tzinfo=timezone.utc),
                results,
            ),
        )

    app = AppTest.from_string(
        _app_script(result_root, cnf_path), default_timeout=20
    ).run(timeout=20)
    assert not app.exception
    html = "\n".join(item.proto.body for item in app.get("html"))
    assert html.count("2026-12") >= 2
    assert html.count("2027-09") >= 2
    assert html.count("M2801") >= 2
    assert html.count("Y2801") >= 2
    assert "margin-null" in html
    assert not app.selectbox

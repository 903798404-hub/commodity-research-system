from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

import pyarrow.parquet as pq
import pytest

from agri_research_agent.data_sources.tankan.models import (
    ConnectionProof,
    QueryPlanProof,
    QuerySpec,
    SourceBatch,
)
from agri_research_agent.data_sources.tankan.queries import FX_WINDOW_QUERY
from agri_research_agent.pipelines import tankan_candidate as candidate_module
from agri_research_agent.pipelines.tankan_candidate import (
    TankanCandidateError,
    build_fx_candidate,
    build_market_candidate,
)


ROOT = Path(__file__).resolve().parents[2]
FX_CONFIG = ROOT / "02_configs" / "tankan_fx.yaml"
NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


class FakeReader:
    def __init__(self, rows: list[dict[str, object]], *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple[QuerySpec, tuple[object, ...], int]] = []
        self.proof = ConnectionProof("quanyong", "18.4", "Asia/Shanghai", "on", "on")

    def plan_stream(
        self,
        query: QuerySpec,
        parameters: Sequence[object],
        *,
        batch_size: int = 10_000,
    ) -> tuple[QueryPlanProof, Iterator[SourceBatch]]:
        values = tuple(parameters)
        self.calls.append((query, values, batch_size))
        plan = QueryPlanProof(
            query.sha256,
            min(20, query.max_plan_rows),
            30.0,
            query.max_plan_rows,
            query.max_total_cost,
        )

        def batches() -> Iterator[SourceBatch]:
            if self.fail:
                raise RuntimeError("stream failed")
            if self.rows:
                yield SourceBatch(query, plan, tuple(self.rows), NOW)

        return plan, batches()


def candidate_root(tmp_path: Path) -> Path:
    return tmp_path / "01_data" / "candidates" / "tankan"


def fx_row() -> dict[str, object]:
    return {
        "trade_date": date(2026, 8, 12),
        "spot": 6.7432,
        **{f"fx_{month}m": 6.74 - month / 100 for month in range(1, 13)},
        "updated_at": datetime(2026, 8, 13, 8, 30),
    }


def test_fx_candidate_is_bounded_sealed_and_manifested_without_promotion(tmp_path: Path) -> None:
    reader = FakeReader([fx_row()])
    result = build_fx_candidate(
        reader,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
        candidate_root=candidate_root(tmp_path),
        candidate_id="fx-20260813",
        mapping_config=FX_CONFIG,
        batch_size=100,
    )

    assert reader.calls[0][1] == (date(2026, 8, 1), date(2026, 8, 13))
    assert result.candidate_directory.name == "fx-20260813"
    assert not list(candidate_root(tmp_path).glob(".building-*"))
    manifest = json.loads(
        (result.candidate_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["dataset_id"] == "tankan.market.exchange_rate"
    assert manifest["provider_dataset_id"] == "tankan:market.exchange_rate"
    assert manifest["candidate_only"] is True
    assert manifest["promotion_authorized"] is False
    assert manifest["canonical_series_ids"] == ["fx.usd.cnh.spot"]
    assert len(manifest["provider_series_ids"]) == 13
    assert manifest["query_parameters"] == {
        "start_date": "2026-08-01",
        "end_date": "2026-08-13",
    }
    assert manifest["connection_proof"]["transaction_read_only"] == "on"
    assert manifest["query_plan"]["explain_analyze"] is False
    assert pq.read_table(result.candidate_directory / "fx_candidate.parquet").num_rows == 13
    spot = pq.read_table(result.candidate_directory / "fx_spot_shadow.parquet").to_pylist()
    assert len(spot) == 1
    assert spot[0]["rate_type"] == "spot"
    assert manifest["shadow_outputs"]["source_policy"]["forward_canonical_promotion"] is False
    assert manifest["shadow_outputs"]["source_policy"]["blocked_forward_row_count"] == 12
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "password" not in serialized.casefold()
    assert not (candidate_root(tmp_path) / "current").exists()


def test_invalid_root_or_window_fails_before_provider_call(tmp_path: Path) -> None:
    reader = FakeReader([fx_row()])
    with pytest.raises(TankanCandidateError, match="candidate_root"):
        build_fx_candidate(
            reader,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 13),
            candidate_root=tmp_path / "processed" / "current",
            candidate_id="unsafe",
            mapping_config=FX_CONFIG,
        )
    with pytest.raises(TankanCandidateError, match="window"):
        build_fx_candidate(
            reader,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 13),
            candidate_root=candidate_root(tmp_path),
            candidate_id="too-wide",
            mapping_config=FX_CONFIG,
        )
    assert reader.calls == []


def test_stream_failure_leaves_no_candidate_or_building_directory(tmp_path: Path) -> None:
    reader = FakeReader([], fail=True)
    root = candidate_root(tmp_path)
    with pytest.raises(RuntimeError, match="stream failed"):
        build_fx_candidate(
            reader,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 13),
            candidate_root=root,
            candidate_id="failed",
            mapping_config=FX_CONFIG,
        )
    assert not (root / "failed").exists()
    assert not list(root.glob(".building-*"))


def test_empty_window_is_blocked_without_sealing(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    with pytest.raises(TankanCandidateError, match="empty input"):
        build_fx_candidate(
            FakeReader([]),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 13),
            candidate_root=root,
            candidate_id="empty",
            mapping_config=FX_CONFIG,
        )
    assert not (root / "empty").exists()
    assert not list(root.glob(".building-*"))


def test_actual_rows_cannot_exceed_hard_query_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bounded = replace(FX_WINDOW_QUERY, max_plan_rows=1)
    monkeypatch.setattr(candidate_module, "FX_WINDOW_QUERY", bounded)
    second = {**fx_row(), "trade_date": date(2026, 8, 13)}
    root = candidate_root(tmp_path)
    with pytest.raises(TankanCandidateError, match="actual row count"):
        build_fx_candidate(
            FakeReader([fx_row(), second]),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 13),
            candidate_root=root,
            candidate_id="too-many-rows",
            mapping_config=FX_CONFIG,
        )
    assert not (root / "too-many-rows").exists()


def test_existing_candidate_rejects_before_provider_query(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    (root / "already-there").mkdir(parents=True)
    reader = FakeReader([fx_row()])
    with pytest.raises(FileExistsError, match="already-there"):
        build_fx_candidate(
            reader,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 13),
            candidate_root=root,
            candidate_id="already-there",
            mapping_config=FX_CONFIG,
        )
    assert reader.calls == []


def test_market_candidate_keeps_provider_identity_and_collision_evidence(tmp_path: Path) -> None:
    mapping = tmp_path / "market.yaml"
    mapping.write_text(
        """schema_version: 1
max_forward_years: 5
required_products: [CANOLA]
series:
  - {source_exchange: ICE, source_product_name: 菜籽, source_series_id: tankan.ffpr.ice.canola.zh, exchange: ICE, product: CANOLA, currency: CAD, price_unit: CAD/metric_tonne, quantity_unit: metric_tonne, contract_size: 20, contract_size_unit: metric_tonne}
  - {source_exchange: ICE, source_product_name: canola, source_series_id: tankan.ffpr.ice.canola.en, exchange: ICE, product: CANOLA, currency: CAD, price_unit: CAD/metric_tonne, quantity_unit: metric_tonne, contract_size: 20, contract_size_unit: metric_tonne}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "trade_date": date(2026, 8, 13),
            "exchange": "ICE",
            "product_name": product,
            "contract": "2611",
            "close_price": price,
            "updated_at": datetime(2026, 8, 14, 8, 30),
        }
        for product, price in (("菜籽", 800.0), ("canola", 802.0))
    ]
    result = build_market_candidate(
        FakeReader(rows),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
        candidate_root=candidate_root(tmp_path),
        candidate_id="market-20260813",
        mapping_config=mapping,
    )
    collision = json.loads(
        (result.candidate_directory / "collision_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert collision["collision_status"] == "BLOCKED_DIFFERING_PROVIDER_VALUES"
    assert collision["promotion_authorized"] is False
    assert {
        item["provider_series_id"]
        for item in collision["samples"][0]["observations"]
    } == {"tankan.ffpr.ice.canola.zh", "tankan.ffpr.ice.canola.en"}


def test_market_candidate_seals_exact_cbot_soybean_shadow_without_cutover(
    tmp_path: Path,
) -> None:
    mapping = tmp_path / "market.yaml"
    mapping.write_text(
        """schema_version: 1
max_forward_years: 5
required_products: [SOYBEAN]
series:
  - {source_exchange: CBOT, source_product_name: 大豆, source_series_id: tankan.ffpr.cbot.soybean.zh, exchange: CBOT, product: SOYBEAN, currency: USD, price_unit: US_cents/bushel, quantity_unit: bushel, contract_size: 5000, contract_size_unit: bushel}
  - {source_exchange: CBOT, source_product_name: soybean, source_series_id: tankan.ffpr.cbot.soybean.en, exchange: CBOT, product: SOYBEAN, currency: USD, price_unit: US_cents/bushel, quantity_unit: bushel, contract_size: 5000, contract_size_unit: bushel}
""",
        encoding="utf-8",
    )
    rows = [
        {
            "trade_date": date(2026, 8, 13),
            "exchange": "CBOT",
            "product_name": product,
            "contract": "2611",
            "close_price": price,
            "updated_at": datetime(2026, 8, 14, 8, 30),
        }
        for product, price in (("大豆", 1_025.5), ("soybean", 1_025.5))
    ]
    result = build_market_candidate(
        FakeReader(rows),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 13),
        candidate_root=candidate_root(tmp_path),
        candidate_id="cbot-soybean-20260813",
        mapping_config=mapping,
    )
    manifest = result.manifest
    shadow = pq.read_table(
        result.candidate_directory / "cbot_soybean_shadow.parquet"
    ).to_pylist()
    assert len(shadow) == 2
    assert {row["provider_role"] for row in shadow} == {
        "current_candidate",
        "legacy_comparison",
    }
    assert manifest["canonical_series_ids"] == [
        "market.quote.cbot.soybean.delivery.close.unknown"
    ]
    assert manifest["shadow_outputs"]["source_policy"]["cutover_authorized"] is False
    assert manifest["shadow_outputs"]["source_policy"]["overlap_equal_key_count"] == 1

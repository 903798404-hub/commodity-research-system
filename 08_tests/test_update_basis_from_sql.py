from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.data_sources.basis_database import DATABASE_COLUMNS
from agri_research_agent.data_sources.basis_sql_loader import (
    PRODUCT_MAP,
    STABLE_KEY,
    sha256_file,
)
from agri_research_agent.pipelines.update_basis_from_sql import (
    CUTOVER_DATE,
    apply_basis_sql_update,
)


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=DATABASE_COLUMNS)
    frame["date"] = pd.to_datetime(frame["date"]).astype("datetime64[us]")
    for column in (
        "commodity",
        "region",
        "quote_type",
        "delivery_month",
        "futures_contract",
        "source_sheet",
    ):
        frame[column] = frame[column].astype("string")
    for column in ("cash_price", "futures_price", "basis"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def _row(
    date: str,
    commodity: str,
    basis: float,
    source_sheet: str,
    *,
    region: str = "华东",
    delivery_month: str = "现货",
    contract: str = "2609",
) -> dict[str, object]:
    return {
        "date": date,
        "commodity": commodity,
        "region": region,
        "quote_type": "基差报价",
        "delivery_month": delivery_month,
        "futures_contract": contract,
        "cash_price": float("nan"),
        "futures_price": float("nan"),
        "basis": basis,
        "source_sheet": source_sheet,
    }


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _config(root: Path, source: Path, history_backup: Path) -> Path:
    path = root / "basis_sql.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "source": {
                    "path": str(source),
                    "table": "basis_price",
                },
                "history": {
                    "source_backup_path": str(history_backup),
                    "source_backup_sha256": sha256_file(history_backup),
                    "baseline_path": str(root / "history" / "baseline.parquet"),
                    "identity_path": str(root / "history" / "baseline.identity.json"),
                    "cutover_date": "2026-06-01",
                },
                "output": {
                    "parquet_path": str(root / "basis_quotes.parquet"),
                    "status_path": str(root / "update_status.json"),
                    "backup_dir": str(root / "backups"),
                    "quality_report_path": str(root / "quality.json"),
                },
                "quality": {
                    "no_valid_group_limit": 0.01,
                    "minimum_latest_date": "2026-08-04",
                    "delivery_month_value": "现货",
                    "near_contract_group_key": [
                        "date",
                        "commodity",
                        "region",
                        "quote_type",
                    ],
                    "primary_key": STABLE_KEY,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _sql_candidate(value: float = 20.0) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for date in ("2026-05-29", "2026-06-01", "2026-08-04"):
        for commodity in PRODUCT_MAP.values():
            rows.append(
                _row(
                    date,
                    commodity,
                    value,
                    f"basis_price:{commodity}",
                    contract="2609",
                )
            )
    return _frame(rows).sort_values(STABLE_KEY).reset_index(drop=True)


def _builder_for(source: Path, candidate_frame: pd.DataFrame):
    def builder(_source: Path, output: Path, **_kwargs: object) -> dict[str, Any]:
        _write_frame(output, candidate_frame)
        digest = sha256_file(source)
        return {
            "source_sha256_before": digest,
            "source_sha256_after": digest,
            "latest_date": pd.Timestamp("2026-08-04"),
            "raw_rows": 100,
            "spot_basis_rows": 90,
            "valid_basis_rows": 80,
            "far_contract_excluded_rows": 3,
            "missing_contract_rows": 1,
            "invalid_contract_rows": 2,
            "expired_contract_rows": 4,
            "no_valid_groups": [],
            "raw_delivery_month_counts": {"现货": 80},
            "merged_delivery_month_groups": [],
        }

    return builder


def _fixtures(root: Path) -> tuple[Path, Path, Path, Path]:
    source = root / "source.sql"
    source.write_text("fixed sql identity\n", encoding="utf-8")
    history_backup = root / "old_backup.parquet"
    old = _frame(
        [
            _row("2026-05-30", "菜粕", 31, "菜粕基差", contract="2609"),
            _row("2026-06-01", "菜粕", 999, "菜粕基差", contract="2609"),
            _row("2026-06-02", "菜粕", 888, "菜粕基差", contract="2609"),
        ]
    )
    _write_frame(history_backup, old)
    formal = root / "basis_quotes.parquet"
    _write_frame(formal, _frame([_row("2026-01-01", "豆粕", 10, "old")]))
    return source, history_backup, formal, _config(root, source, history_backup)


def test_builder_failure_does_not_replace_formal_parquet(tmp_path: Path) -> None:
    _source, _history, formal, config = _fixtures(tmp_path)
    before = formal.read_bytes()

    def fail_builder(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated candidate failure")

    with pytest.raises(RuntimeError, match="simulated candidate failure"):
        apply_basis_sql_update(
            config_path=config,
            project_root=tmp_path,
            builder=fail_builder,
        )
    assert formal.read_bytes() == before
    assert not (tmp_path / "backups").exists()


def test_fixed_cutover_builds_immutable_history_and_never_falls_back(
    tmp_path: Path,
) -> None:
    source, history_backup, formal, config = _fixtures(tmp_path)
    sql = _sql_candidate()

    result = apply_basis_sql_update(
        config_path=config,
        project_root=tmp_path,
        builder=_builder_for(source, sql),
    )
    merged = pd.read_parquet(formal)
    dates = pd.to_datetime(merged["date"])
    before = merged.loc[dates < CUTOVER_DATE]
    after = merged.loc[dates >= CUTOVER_DATE]

    assert CUTOVER_DATE == pd.Timestamp("2026-06-01")
    assert before[["date", "basis", "source_sheet"]].to_dict("records") == [
        {
            "date": pd.Timestamp("2026-05-30"),
            "basis": 31.0,
            "source_sheet": "菜粕基差",
        }
    ]
    assert after["source_sheet"].astype(str).str.startswith("basis_price:").all()
    assert not merged["date"].eq(pd.Timestamp("2026-06-02")).any()
    assert merged.duplicated(STABLE_KEY).sum() == 0
    assert result["history_rows"] == 1
    assert result["sql_after_cutover_rows"] == 10
    assert result["rows"] == 11

    baseline = tmp_path / "history" / "baseline.parquet"
    identity_path = tmp_path / "history" / "baseline.identity.json"
    baseline_before = baseline.read_bytes()
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert pd.read_parquet(baseline)["date"].max() < CUTOVER_DATE
    assert identity["source_backup_sha256"] == sha256_file(history_backup)
    assert identity["history_baseline_sha256"] == sha256_file(baseline)
    assert pq.read_schema(formal).remove_metadata() == pq.read_schema(
        history_backup
    ).remove_metadata()

    history_backup.unlink()
    source.write_text("next sql identity\n", encoding="utf-8")
    apply_basis_sql_update(
        config_path=config,
        project_root=tmp_path,
        builder=_builder_for(source, _sql_candidate(value=25.0)),
    )
    assert baseline.read_bytes() == baseline_before
    refreshed = pd.read_parquet(formal)
    assert refreshed.loc[refreshed["date"] < CUTOVER_DATE, "basis"].tolist() == [31.0]
    assert set(refreshed.loc[refreshed["date"] >= CUTOVER_DATE, "basis"]) == {25.0}

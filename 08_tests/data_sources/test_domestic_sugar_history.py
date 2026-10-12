from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from agri_research_agent.data_sources.domestic_sugar_history import (
    SPOT_SERIES, SugarHistoryError, file_sha256, load_sugar_history,
)
from agri_research_agent.pipelines.domestic_sugar_history import prepare_sugar_history


@pytest.fixture
def source_directory(tmp_path: Path) -> Path:
    source = tmp_path / "approved-source"
    source.mkdir()
    cash = {"2023-01-03": 6100, "2024-02-29": 6000, "2024-03-01": None,
            "2026-09-30": 5180, "2026-10-09": 5280, "2026-10-10": 5330}
    spot = [{"series_id": SPOT_SERIES, "business_date": day, "cash_price": value,
             "source_cell": f"C{i+5}", "status": "OBSERVED" if value is not None else "SOURCE_NULL"} for i, (day, value) in enumerate(cash.items())]
    fut = [{"ts_code": code, "business_date": day, "close": close} for code, day, close in (
        ("SR2501.ZCE", "2024-02-29", 5000), ("SR2501.ZCE", "2024-03-01", 5010),
        ("SR2701.ZCE", "2026-09-30", 5354), ("SR2701.ZCE", "2026-10-09", 5491),
        ("SR2705.ZCE", "2026-10-09", 5568), ("SR2709.ZCE", "2026-10-09", 5650))]
    extra = [{"ts_code": "SR2303.ZCE", "business_date": "2023-01-03", "close": 5700}]
    mapping = [{"business_date": day, "mapped_contract": code} for day, code in (
        ("2023-01-03", "SR2303.ZCE"), ("2024-02-29", "SR2501.ZCE"),
        ("2024-03-01", "SR2501.ZCE"), ("2026-09-30", "SR2701.ZCE"), ("2026-10-09", "SR2701.ZCE"))]
    fixed = []
    for f in fut:
        value = cash[f["business_date"]]
        fixed.append(dict(series_id=SPOT_SERIES, ts_code=f["ts_code"], business_date=f["business_date"],
            cash_price=value, futures_price=f["close"], basis=value - f["close"] if value is not None else None,
            delivery_year=2000 + int(f["ts_code"][2:4]), delivery_month=int(f["ts_code"][4:6]), futures_price_field="close"))
    nearest = [dict(business_date=r["business_date"], delivery_month=r["delivery_month"], ts_code=r["ts_code"],
                    cash_price=r["cash_price"], futures_price=r["futures_price"], basis=r["basis"]) for r in fixed]
    nearest.extend([dict(business_date="2026-10-10", delivery_month=month, ts_code=f"SR27{month:02d}.ZCE",cash_price=5330,futures_price=None,basis=None) for month in (1,5,9)])
    indexes = {(r["ts_code"], r["business_date"]): r["close"] for r in [*fut,*extra]}
    mapped = {r["business_date"]: r["mapped_contract"] for r in mapping}
    main = []
    for day, value in cash.items():
        code = mapped.get(day); close = indexes.get((code,day))
        main.append(dict(business_date=day,ts_code=code,cash_price=value,futures_price=close,
                         basis=value-close if value is not None and close is not None else None,price_field="close"))
    with sqlite3.connect(source/"白糖柳州历史库.sqlite3") as db:
        for name, rows in {"spot_daily":spot,"futures_daily":fut,"main_mapping":mapping,
                           "main_supplemental_daily":extra,"basis_fixed_contract":fixed,
                           "basis_nearest_month":nearest,"basis_main_continuous":main}.items():
            pd.DataFrame(rows).to_sql(name,db,index=False)
    contract=dict(main_spot_series=dict(series_id=SPOT_SERIES,unit="CNY/tonne",source_sha256="a"*64,
                                       source_sheet="导出数据",source_column="C",definition="当地贸易商报价平均值"),
                  futures=dict(field="close",endpoint="https://approved-gateway.example/",source="approved gateway capture",response_file_sha256="b"*64))
    (source/"history_contract.json").write_text(json.dumps(contract,ensure_ascii=False),encoding="utf-8")
    (source/"audit_summary.json").write_text(json.dumps({"as_of":"2026-10-12"}),encoding="utf-8")
    _reseal_source(source)
    return source


def _reseal_source(source):
    database=source/"白糖柳州历史库.sqlite3"
    (source/"verification.json").write_text(json.dumps(dict(result="PASS",files=[dict(file=database.name,sha256=file_sha256(database))])),encoding="utf-8")


@pytest.fixture
def prepared_root(source_directory: Path, tmp_path: Path) -> Path:
    root=tmp_path/"candidate"
    prepare_sugar_history(source_directory,root)
    return root


def test_preparation_preserves_input_and_repeats_without_writes(source_directory, tmp_path):
    original=file_sha256(source_directory/"白糖柳州历史库.sqlite3")
    root=tmp_path/"candidate"
    assert prepare_sugar_history(source_directory,root)["status"]=="PREPARED"
    before={str(p.relative_to(root)):file_sha256(p) for p in root.rglob("*") if p.is_file()}
    assert prepare_sugar_history(source_directory,root)["status"]=="NO_CHANGE"
    assert before=={str(p.relative_to(root)):file_sha256(p) for p in root.rglob("*") if p.is_file()}
    assert file_sha256(source_directory/"白糖柳州历史库.sqlite3")==original


def test_reader_keeps_missing_dates_and_actual_03_main(prepared_root):
    snapshot=load_sugar_history(prepared_root)
    oct10=snapshot.nearest_basis.loc[snapshot.nearest_basis.business_date.eq("2026-10-10")]
    assert oct10.cash_price.eq(5330).all() and oct10.basis.isna().all() and oct10.futures_price.isna().all()
    early=snapshot.main_basis.loc[snapshot.main_basis.business_date.eq("2023-01-03")].iloc[0]
    assert early.ts_code=="SR2303.ZCE" and early.basis==400
    assert snapshot.main_basis.loc[snapshot.main_basis.business_date.eq("2024-03-01"),"basis"].isna().all()


@pytest.mark.parametrize("mutation,reason",[
    ("UPDATE basis_fixed_contract SET basis=basis+1 WHERE basis IS NOT NULL", "基差"),
    ("INSERT INTO spot_daily SELECT * FROM spot_daily LIMIT 1", "重复"),
    ("UPDATE basis_main_continuous SET ts_code='SR2705.ZCE' WHERE business_date='2026-10-09'", "合约"),
    ("UPDATE basis_nearest_month SET futures_price=5491,basis=5330-5491 WHERE business_date='2026-10-10'", "同日"),
    ("UPDATE basis_fixed_contract SET cash_price=6001,basis=1001 WHERE business_date='2024-02-29'", "原始"),
    ("UPDATE basis_fixed_contract SET delivery_year=2024 WHERE ts_code='SR2501.ZCE'", "年份"),
    ("UPDATE basis_fixed_contract SET futures_price_field='settle'", "收盘"),
])
def test_invalid_source_is_rejected_before_candidate_creation(source_directory,tmp_path,mutation,reason):
    with sqlite3.connect(source_directory/"白糖柳州历史库.sqlite3") as db:db.execute(mutation)
    _reseal_source(source_directory)  # Exercise semantic validation behind a valid file identity.
    root=tmp_path/"invalid"
    with pytest.raises(SugarHistoryError,match=reason):prepare_sugar_history(source_directory,root)
    assert not (root/"current.json").exists()


def test_corrupt_snapshot_does_not_fall_back(prepared_root):
    pointer=json.loads((prepared_root/"current.json").read_text())
    database=prepared_root/"snapshots"/pointer["snapshot_id"]/"history.sqlite3"
    with database.open("ab") as file:file.write(b"changed")
    with pytest.raises(SugarHistoryError,match="身份"):load_sugar_history(prepared_root)


def test_wrong_unit_even_with_rehashed_manifest_is_rejected(prepared_root):
    pointer=json.loads((prepared_root/"current.json").read_text())
    path=prepared_root/"snapshots"/pointer["snapshot_id"]/"manifest.json"
    manifest=json.loads(path.read_text(encoding="utf-8"));manifest["unit"]="USD/tonne"
    path.write_text(json.dumps(manifest),encoding="utf-8")
    pointer["manifest_sha256"]=file_sha256(path)
    (prepared_root/"current.json").write_text(json.dumps(pointer),encoding="utf-8")
    with pytest.raises(SugarHistoryError,match="单位"):load_sugar_history(prepared_root)


def test_pointer_traversal_and_duplicate_fields_fail_closed(prepared_root):
    pointer=prepared_root/"current.json"
    data=json.loads(pointer.read_text());data["snapshot_id"]="../../other"
    pointer.write_text(json.dumps(data))
    with pytest.raises(SugarHistoryError,match="身份"):load_sugar_history(prepared_root)
    pointer.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(SugarHistoryError,match="重复"):load_sugar_history(prepared_root)


def test_missing_root_never_generates_a_database(tmp_path):
    with pytest.raises(SugarHistoryError):load_sugar_history(tmp_path/"missing")
    assert not (tmp_path/"missing").exists()


def test_null_pointer_identity_is_a_controlled_reader_error(prepared_root):
    pointer=prepared_root/"current.json"
    data=json.loads(pointer.read_text());data["snapshot_id"]=None
    pointer.write_text(json.dumps(data))
    with pytest.raises(SugarHistoryError,match="身份"):load_sugar_history(prepared_root)


def test_unreviewed_source_changes_cannot_be_prepared(source_directory,tmp_path):
    with sqlite3.connect(source_directory/"白糖柳州历史库.sqlite3") as db:
        db.execute("UPDATE spot_daily SET cash_price=6001 WHERE business_date='2024-02-29'")
    with pytest.raises(SugarHistoryError,match="来源身份"):
        prepare_sugar_history(source_directory,tmp_path/"changed-source")

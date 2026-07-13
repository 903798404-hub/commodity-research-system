import datetime as dt
from pathlib import Path
import pandas as pd
from agri_research_agent.data_sources.basis_entry import adapt_excel_result, config, parse_fixed_rows, parse_paste, parse_text, write_confirmed

CFG = config(Path(__file__).parents[1] / "02_configs" / "basis_varieties.yaml")

def test_aliases_symbols_contracts_and_inference():
    d = parse_text("张家港豆油09+320\n广东棕油09升水120\n广西菜籽油09贴水50\n东莞豆粕 M2609+80\n广西菜粕 RM2609-30", dt.date(2026,7,11), "test", CFG)
    assert d.variety_code.tolist() == ["Y","P","OI","M","RM"]
    assert d.basis.tolist() == [320,120,-50,80,-30]
    assert d.futures_contract.tolist() == ["Y2609","P2609","OI2609","M2609","RM2609"]
    assert d.contract_inferred.iloc[:3].all() and (d.parse_status.iloc[:3] == "needs_review").all()

def test_invalid_and_atomic_duplicate_write(tmp_path: Path):
    invalid = parse_text("上海玉米 C09+10", dt.date(2026,7,11), "test", CFG)
    assert invalid.parse_status.iloc[0] == "invalid"
    data = parse_text("2026-07-11 张家港 豆油 Y2609+320", dt.date(2026,7,11), "test", CFG)
    path = tmp_path / "basis.parquet"
    first = write_confirmed(data, path, tmp_path / "backups")
    second = write_confirmed(data, path, tmp_path / "backups")
    assert first["success"] == 1 and second["skipped"] == 1 and len(pd.read_parquet(path)) == 1

def test_fixed_paste_formats_and_no_contract_inference():
    for delimiter in ["|", "\t", ","]:
        d = parse_paste(delimiter.join(["张家港","豆油","Y2609","320","8200","一级豆油","备注"]), dt.date(2026,7,11), "test", CFG)
        assert d.parse_status.iloc[0] == "parsed" and not d.contract_inferred.iloc[0]
    bad = parse_paste("张家港|豆油|Y2609|320", dt.date(2026,7,11), "test", CFG)
    assert bad.parse_status.iloc[0] == "invalid"

def test_table_entry_shared_standardization():
    raw = pd.DataFrame([["广西","菜油","OI2609",-50,None,"三级菜油",""]], columns=["地区","品种","基准合约","基差","一口价","报价名称","备注"])
    result = parse_fixed_rows(raw, dt.date(2026,7,11), "test", CFG, "table")
    assert result.import_method.iloc[0] == "table" and result.variety_code.iloc[0] == "OI"

def test_excel_adapter_uses_shared_validation_and_write(tmp_path: Path):
    legacy = pd.DataFrame([{"date":"2026-07-11","commodity":"豆油","region":"张家港","futures_contract":"Y2609","basis":320,"cash_price":8200,"source_sheet":"豆油基差"}])
    result = adapt_excel_result(legacy, "old.xlsx", CFG)
    assert result.import_method.iloc[0] == "excel" and result.variety_code.iloc[0] == "Y"
    output = tmp_path / "basis.parquet"
    write_confirmed(result, output, tmp_path / "backup")
    assert len(pd.read_parquet(output)) == 1

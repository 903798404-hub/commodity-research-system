import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "04_scripts"))
from verify_domestic_sugar_daily import verify


def fixture(path):
    book = openpyxl.Workbook(); sheet = book.active; sheet.title = "钢联原始数据"
    sheet["A5"] = dt.datetime(2026, 11, 10); sheet["J5"] = 5150; sheet["K5"] = 5160
    book.save(path / "workbook.xlsx")
    mapping = dict(code=0, data=dict(fields=["ts_code", "trade_date", "mapping_ts_code"],
                                    items=[["SR.ZCE", "20261110", "SR2701.ZCE"]]))
    daily = [dict(code=0, data=dict(fields=["ts_code", "trade_date", "close"],
                                  items=[["SR2701.ZCE", "20261110", 5195]]))]
    for name, value in (("mapping.json", mapping), ("daily.json", daily)):
        (path / name).write_text(json.dumps(value), encoding="utf-8")
    sha = lambda name: hashlib.sha256((path / name).read_bytes()).hexdigest()
    records = [dict(date="2026-11-10", region=r, contract="SR2701.ZCE", close=5195,
                    components=([dict(name="东糖", price=5150, cell="J5"),
                                 dict(name="凤糖", price=5160, cell="K5")] if r == "广西糖厂" else []))
               for r in ("广西糖厂", "昆明", "乌鲁木齐")]
    data = dict(schema_version="domestic-sugar-daily/1", workbook_sha256=sha("workbook.xlsx"),
                mapping_sha256=sha("mapping.json"), daily_sha256=sha("daily.json"), records=records)
    (path / "feed.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def test_candidate_verifies_source_and_preserves_published(tmp_path):
    data = fixture(tmp_path)
    result = verify(tmp_path)
    assert result["valid_spot"] == 1 and result["paired"] == 1
    original = tmp_path / "current.json"
    original.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    data["records"][0]["components"].pop()
    (tmp_path / "feed.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="样本不符"):
        verify(tmp_path, original)

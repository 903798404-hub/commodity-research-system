from datetime import date
from pathlib import Path

from openpyxl import Workbook
import pytest

from agri_research_agent.data_sources import nutstore_basis as source


def workbook(monkeypatch, tmp_path, records):
    path = tmp_path / "123/国内基差及一口价/基差数据.xlsx"
    path.parent.mkdir(parents=True)
    book = Workbook()
    sheet = book.active
    sheet.title = source.SOURCE_SHEET
    sheet.append(source.COLUMNS)
    for record in records:
        sheet.append([record.get(field) for field in source.COLUMNS])
    book.save(path)
    book.close()
    monkeypatch.setattr(source, "PROTECTED_ROOT", tmp_path / "123")
    monkeypatch.setattr(source, "SOURCE_PATH", path)
    return path


def row(**changes):
    value = {"日期": date(2026, 9, 29), "品种": "豆粕", "地区": "华东",
             "报价类别": "现货基差", "期货合约": "2701", "基差": 0}
    value.update(changes)
    return value


def test_same_contract_median_zero_and_cash_null_semantics(monkeypatch, tmp_path):
    path = workbook(monkeypatch, tmp_path, [
        row(), row(基差=100), row(期货合约="2705", 基差=900),
        row(期货合约="2605", 基差=500), row(基差=None),
        row(报价类别="一口价", 期货合约=None, 基差=None, 现货价=3300),
        row(报价类别="远月基差", 基差=999),
        row(日期=date(2026, 9, 2), 基差=999),
    ])
    before = source.sha256(path)
    result = source.read_nutstore_basis(path, after=date(2026, 9, 3))
    basis = next(item for item in result.rows if item["quote_type"] == "基差报价")
    cash = next(item for item in result.rows if item["quote_type"] == "一口价")
    assert basis["basis"] == 50 and basis["futures_contract"] == "2701"
    assert basis["cash_price"] is None and basis["futures_price"] is None
    assert basis["source_row_count"] == 2 and basis["far_contract_row_count"] == 1
    assert cash["cash_price"] == 3300 and cash["basis"] is None
    assert cash["futures_contract"] is None
    assert source.sha256(path) == before == result.report["source_sha256"]
    assert result.report["counts"]["zero_value_rows"] == 1
    assert result.report["counts"]["missing_value_rows"] == 1
    assert result.report["counts"]["preserved_history_raw_rows"] == 1


def test_protected_tree_and_other_source_paths_are_rejected(monkeypatch, tmp_path):
    path = workbook(monkeypatch, tmp_path, [row()])
    for target in (tmp_path / "123", path.parent / "candidate", tmp_path / "123/unrelated/log.json"):
        with pytest.raises(ValueError, match="forbidden"):
            source.assert_external_output(target)
    with pytest.raises(ValueError, match="allowed"):
        source.read_nutstore_basis(path.parent / "another.xlsx", after=date(2026, 9, 3))
    assert source.assert_external_output(tmp_path / "external") == tmp_path / "external"
    assert not (tmp_path / "123/unrelated").exists()


@pytest.mark.parametrize("changes", [
    {"日期": date(2099, 1, 1)}, {"基差": "=1+2"}, {"基差": "unknown"},
    {"地区": None}, {"品种": "未知"},
])
def test_invalid_raw_values_fail_without_source_changes(monkeypatch, tmp_path, changes):
    path = workbook(monkeypatch, tmp_path, [row(**changes)])
    before = source.sha256(path)
    with pytest.raises(ValueError):
        source.read_nutstore_basis(path, after=date(2026, 9, 3))
    assert source.sha256(path) == before


def test_source_changed_while_reading_is_rejected(monkeypatch, tmp_path):
    path = workbook(monkeypatch, tmp_path, [row()])
    identities = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(source, "sha256", lambda _: next(identities))
    with pytest.raises(ValueError, match="changed"):
        source.read_nutstore_basis(path, after=date(2026, 9, 3))


def test_junction_in_output_path_is_rejected_before_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "is_junction", lambda node: node == tmp_path / "redirect")
    with pytest.raises(ValueError, match="junction"):
        source.assert_external_output(tmp_path / "redirect/log")
    assert not (tmp_path / "redirect").exists()

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from agri_research_agent.sugar_import_profit import history, model, runtime
from agri_research_agent.sugar_import_profit.store import Store, DDL


def sources():
    return {"巴西": dict(filename="巴西原表.xlsx", rows=[["2025-10-01", 0, 0, 29, 3348, None, 4252, -100]]),
            "泰国": dict(filename="泰国原表.xlsx", rows=[["2025-10-01", 3798, 0, 4838, -200, 10, -1]]),
            "日照": dict(filename="日照原表.xlsx", rows=[["2025-10-01", 5000]])}


def quote():
    value = history.defaults(sources(), "巴西", "2025-10-01")
    value.update(ice=17., insurance_fee=0., fx=7., contract="SBH26", ice_date="2025-10-01", fx_date="2025-10-01")
    return value


def bundle(root):
    root.mkdir(parents=True)
    raw = json.dumps(sources(), ensure_ascii=False).encode("utf-8")
    (root / "steel_sources.json").write_bytes(raw)
    (root / "manifest.json").write_text(json.dumps(dict(schema_version=history.SCHEMA, source_bytes=len(raw),
        source_sha256=hashlib.sha256(raw).hexdigest(), currency="CNY", profit_unit="yuan/tonne",
        row_counts={key: len(value["rows"]) for key, value in sources().items()})), encoding="utf-8")


def test_formula_matches_accepted_model_zero_and_two_loss_definitions():
    value = model.validate_inputs(quote())
    result = model.calculate(value)
    fob = 17 * 1.042 * 22.046
    taxed = (fob + 29) * 7 * 1.15 * 1.13
    assert result["cost_in"] == pytest.approx(taxed * 1.04 + 400)
    assert result["profit_out"] == pytest.approx(5000 - ((fob + 29) * 7 * 1.5 * 1.13 * 1.04 + 400))
    value["loss_mode"] = "实际损耗"
    assert model.calculate(value)["cost_in"] == pytest.approx(taxed / .96 + 400)


@pytest.mark.parametrize("change", [{"fx": None}, {"ice": True}, {"fx": float("nan")},
                                    {"ice_date": "2025-10-02"}, {"contract": ""}, {"example": True}])
def test_incomplete_future_or_example_cannot_save(tmp_path, change):
    value = dict(quote(), **change)
    path = tmp_path / "never.sqlite3"
    with pytest.raises(ValueError):
        Store(path, lambda _: None).save("巴西", value, expected_version=None)
    assert not path.exists()


def test_history_hash_null_zero_and_no_future_default(tmp_path):
    root = tmp_path / "history"
    bundle(root)
    src, manifest = history.load_sources(root)
    rows = history.history_rows(src)
    assert rows[0]["profit_in"] is None
    assert rows[1]["profit_in"] == 0
    old = history.defaults(src, "巴西", "2020-01-01")
    assert old["premium"] is old["domestic"] is None
    (root / "steel_sources.json").write_bytes((root / "steel_sources.json").read_bytes() + b" ")
    with pytest.raises(ValueError, match="校验"):
        history.load_sources(root)


def test_duplicates_invalid_numbers_and_date_range():
    src = sources()
    src["巴西"]["rows"] *= 2
    with pytest.raises(ValueError, match="重复"):
        history.validate_sources(src)
    with pytest.raises(ValueError):
        history.history_rows(sources(), "2026-01-01", "2025-01-01")


def test_transactional_idempotence_revision_and_concurrent_sessions(tmp_path):
    calls = []
    path = tmp_path / "own.sqlite3"
    store = Store(path, lambda p: calls.append(p))
    assert not path.exists() and store.records() == []
    q = quote()
    saved = store.save("巴西", q, expected_version=None)
    assert saved["status"] == "saved"
    original = store.records()[0]
    assert saved["version"] == original["version"]
    assert store.save("巴西", q, expected_version=None)["status"] == "unchanged"
    q["fx"] = 7.1
    assert store.save("巴西", q, expected_version=original["version"])["status"] == "conflict"
    assert store.save("巴西", q, expected_version=original["version"], revise=True)["status"] == "revised"
    latest = store.records()[0]
    assert latest["version"] != original["version"]
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from records").fetchone()[0] == 1
        row = db.execute("select inputs,results from revisions").fetchone()
        assert json.loads(row[0]) == original["inputs"]
        assert json.loads(row[1]) == original["results"]
    with pytest.raises(ValueError, match="另一会话"):
        store.save("巴西", dict(q, fx=7.2), expected_version=original["version"], revise=True)
    assert store.records()[0] == latest
    assert calls


def test_legacy_sqlite_read_and_no_authority_no_write(tmp_path):
    path = tmp_path / "old.sqlite3"
    q = model.validate_inputs(quote())
    with sqlite3.connect(path) as db:
        db.executescript(DDL)
        db.execute("insert into records(date,country,inputs,results,model_version,saved_at) values(?,?,?,?,?,?)",
                   (q["date"], "巴西", json.dumps(q), json.dumps(model.calculate(q)), model.MODEL_VERSION, "legacy-time"))
    raw = path.read_bytes()
    def deny(_):
        raise ValueError("没有权限")
    store = Store(path, deny)
    original = store.records()[0]
    assert original["saved_at"] == "legacy-time"
    with pytest.raises(ValueError, match="权限"):
        store.save("巴西", dict(q, fx=7.2), expected_version=original["version"], revise=True)
    assert path.read_bytes() == raw


def test_note_cas_and_independent_business_keys(tmp_path):
    store = Store(tmp_path / "own.sqlite3", lambda _: None)
    store.save("巴西", quote(), expected_version=None)
    store.save("泰国", quote(), expected_version=None)
    store.save_note(dict(text="研究", date="2025-10-01"), None)
    note = store.setting("research-note")
    with pytest.raises(ValueError, match="笔记已变化"):
        store.save_note(dict(text="旧会话", date="2025-10-01"), None)
    assert store.setting("research-note") == note
    assert len(store.records()) == 2


def test_runtime_fail_closed_and_dedicated_local_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SOURCE", tmp_path)
    monkeypatch.setattr(runtime, "deployed", lambda: False)
    monkeypatch.delenv("SUGAR_IMPORT_ALLOW_SAVE", raising=False)
    monkeypatch.setenv("SUGAR_IMPORT_LOCAL_ROOT", str(tmp_path / "01_data/candidates/sugar_import_profit/test"))
    monkeypatch.setenv("SUGAR_IMPORT_LOCAL_PREVIEW", "1")
    path, _, auth, enabled = runtime.storage_context()
    assert enabled
    auth(path)
    with pytest.raises(ValueError):
        auth(tmp_path / "some-other.sqlite3")
    monkeypatch.setenv("SUGAR_IMPORT_ALLOW_SAVE", "1")
    with pytest.raises(ValueError, match="正式身份"):
        runtime.storage_context()


def test_prepare_preserves_complete_legacy_database_and_source(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "04_scripts/import_profit/prepare_sugar_import_history.py"
    spec = importlib.util.spec_from_file_location("sugar_prepare", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(sources()), encoding="utf-8")
    db = tmp_path / "legacy.sqlite3"
    store = Store(db, lambda _: None)
    store.save("巴西", quote(), expected_version=None)
    old = store.records()[0]
    store.save("巴西", dict(quote(), fx=7.2), expected_version=old["version"], revise=True)
    store.save_note(dict(text="保留全部笔记", date="2025-10-01"), None)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, db)}
    dest = tmp_path / "01_data/candidates/sugar_import_profit/new"
    result = module.prepare(source, before[source], db, before[db], dest)
    assert result["sqlite_counts"] == dict(records=1, revisions=1, settings=2)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    with sqlite3.connect(db) as a, sqlite3.connect(dest / "sugar-import.sqlite3") as b:
        for table in ("settings", "records", "revisions"):
            assert a.execute(f"select * from {table}").fetchall() == b.execute(f"select * from {table}").fetchall()
    history.load_sources(dest / "history")
    with pytest.raises(ValueError, match="独立新建"):
        module.prepare(source, before[source], db, before[db], dest)

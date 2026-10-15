"""Prepare an isolated history bundle and a lossless local SQLite backup; no publication."""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))
from agri_research_agent.sugar_import_profit.history import SCHEMA, validate_sources
from agri_research_agent.sugar_import_profit.model import MODEL_VERSION


def prepare(source_file: Path, source_sha256: str, database: Path, database_sha256: str, destination: Path):
    source_file, database, destination = map(lambda p: Path(p).absolute(), (source_file, database, destination))
    base = ROOT / "01_data/candidates/sugar_import_profit"
    if (destination.resolve() != destination or not destination.is_relative_to(base)
            or destination == base or destination.exists()):
        raise ValueError("白糖候选目录必须独立新建")
    for path, digest in ((source_file, source_sha256), (database, database_sha256)):
        if path.resolve() != path or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("白糖原始输入身份不符")
    raw = source_file.read_bytes()
    sources = validate_sources(json.loads(raw.decode("utf-8")))
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source_db:
        source_db.execute("BEGIN")
        if source_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("本机白糖数据库损坏")
        tables = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        legacy = {"records", "revisions", "settings"}
        if tables not in (legacy, legacy | {"monthly_records", "monthly_revisions"}):
            raise ValueError("本机白糖数据库结构不符")
        if source_db.execute("SELECT count(*) FROM records WHERE model_version != ?", (MODEL_VERSION,)).fetchone()[0]:
            raise ValueError("本机白糖记录模型版本未支持")
        counts = {table: source_db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in sorted(tables)}
        destination.mkdir(parents=True)
        with closing(sqlite3.connect(destination / "sugar-import.sqlite3")) as target:
            source_db.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        with closing(sqlite3.connect((destination / "sugar-import.sqlite3").as_uri() + "?mode=ro", uri=True)) as target:
            for table in tables:
                if source_db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() != target.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall():
                    raise ValueError("白糖数据库备份内容不一致")
    for path, digest in ((source_file, source_sha256), (database, database_sha256)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("白糖原始输入在准备期间变化")
    history = destination / "history"
    history.mkdir()
    (history / "steel_sources.json").write_bytes(raw)
    manifest = dict(schema_version=SCHEMA, source_sha256=source_sha256, source_bytes=len(raw),
                    currency="CNY", profit_unit="yuan/tonne", source="钢联原表",
                    row_counts={k: len(v["rows"]) for k, v in sources.items()})
    (history / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = dict(schema_version="sugar-import-preparation/1", source_path=str(source_file),
                  original_database_path=str(database), original_database_sha256=database_sha256,
                  destination=str(destination), sqlite_counts=counts, original_inputs_unchanged=True,
                  auto_update=False, production_publication=False,
                  files={str(p.relative_to(destination)): dict(bytes=p.stat().st_size,
                         sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in destination.rglob("*") if p.is_file()})
    (destination / "preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source-file", "source-sha256", "database", "database-sha256", "destination"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(Path(args.source_file), args.source_sha256, Path(args.database),
                             args.database_sha256, Path(args.destination)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

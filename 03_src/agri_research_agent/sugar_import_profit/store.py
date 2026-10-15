"""Separate SQLite, compatible with local records, authorized writes and row CAS."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from .model import COUNTRIES, MODEL_VERSION, calculate, timestamp, validate_inputs

DDL = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY,date TEXT NOT NULL,country TEXT NOT NULL,
 inputs TEXT NOT NULL,results TEXT NOT NULL,model_version TEXT NOT NULL,saved_at TEXT NOT NULL,
 UNIQUE(date,country));
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,date TEXT NOT NULL,country TEXT NOT NULL,
 inputs TEXT NOT NULL,results TEXT NOT NULL,model_version TEXT NOT NULL,old_saved_at TEXT NOT NULL,
 revised_at TEXT NOT NULL);
"""

MONTHLY_DDL = """
CREATE TABLE IF NOT EXISTS monthly_records(date TEXT NOT NULL,country TEXT NOT NULL,
 payload TEXT NOT NULL,sha256 TEXT NOT NULL,saved_at TEXT NOT NULL,PRIMARY KEY(date,country));
CREATE TABLE IF NOT EXISTS monthly_revisions(id INTEGER PRIMARY KEY,date TEXT NOT NULL,country TEXT NOT NULL,
 payload TEXT NOT NULL,sha256 TEXT NOT NULL,saved_at TEXT NOT NULL);
"""


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def version(row):
    if row is None:
        return None
    return hashlib.sha256(packed(dict(row)).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path, authorize):
        self.path, self.authorize = Path(path), authorize

    @contextmanager
    def read(self):
        if not self.path.exists():
            yield None
            return
        if self.path.resolve() != self.path.absolute():
            raise ValueError("白糖业务库不能使用路径别名")
        db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        self.authorize(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Verify again after directory creation; authorization fails closed.
        self.authorize(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(DDL)
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def records(self):
        with self.read() as db:
            rows = db.execute("SELECT * FROM records ORDER BY date DESC,country").fetchall() if db else []
        return [dict(id=r["id"], date=r["date"], country=r["country"],
                     inputs=json.loads(r["inputs"]), results=json.loads(r["results"]),
                     model_version=r["model_version"], saved_at=r["saved_at"], version=version(r)) for r in rows]

    def setting(self, key, default=None):
        with self.read() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() if db else None
        return json.loads(row["value"]) if row else default

    def save_note(self, value, expected):
        if not isinstance(value, dict) or not isinstance(value.get("text"), str) or len(value["text"]) > 10000:
            raise ValueError("研究笔记无效")
        from datetime import date
        date.fromisoformat(value["date"])
        with self.write() as db:
            row = db.execute("SELECT value FROM settings WHERE key='research-note'").fetchone()
            old = json.loads(row["value"]) if row else None
            if old != expected:
                raise ValueError("研究笔记已变化，请刷新后重新核对")
            saved = dict(value, saved_at=timestamp())
            db.execute("INSERT INTO settings VALUES('research-note',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (packed(saved),))
        return saved

    def save(self, country, raw, *, expected_version, revise=False):
        if country not in COUNTRIES:
            raise ValueError("国家无效")
        inputs = validate_inputs(raw)
        results = calculate(inputs)
        with self.write() as db:
            old = db.execute("SELECT * FROM records WHERE date=? AND country=?", (inputs["date"], country)).fetchone()
            if old and json.loads(old["inputs"]) == inputs:
                return dict(status="unchanged", results=results, version=version(old))
            if version(old) != expected_version:
                raise ValueError("当日记录已被另一会话修改，请刷新后核对")
            if old and not revise:
                return dict(status="conflict", old=json.loads(old["inputs"]), new=inputs)
            stamp = timestamp()
            if old:
                db.execute("INSERT INTO revisions(date,country,inputs,results,model_version,old_saved_at,revised_at) VALUES(?,?,?,?,?,?,?)",
                           (old["date"], country, old["inputs"], old["results"], old["model_version"], old["saved_at"], stamp))
            db.execute("INSERT INTO records(date,country,inputs,results,model_version,saved_at) VALUES(?,?,?,?,?,?) ON CONFLICT(date,country) DO UPDATE SET inputs=excluded.inputs,results=excluded.results,model_version=excluded.model_version,saved_at=excluded.saved_at",
                       (inputs["date"], country, packed(inputs), packed(results), MODEL_VERSION, stamp))
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("draft:" + country, packed(dict(inputs, example=False))))
            saved_version = version(db.execute("SELECT * FROM records WHERE date=? AND country=?",
                                              (inputs["date"], country)).fetchone())
        return dict(status="revised" if old else "saved", results=results, version=saved_version)

    def monthly_records(self):
        from .model import validate_monthly
        with self.read() as db:
            if not db or not db.execute("SELECT 1 FROM sqlite_master WHERE name='monthly_records'").fetchone():
                return []
            rows = db.execute("SELECT date,country,CASE WHEN length(CAST(payload AS BLOB))<=262144 THEN payload END AS payload,sha256,saved_at FROM monthly_records ORDER BY date DESC,country LIMIT 4097").fetchall()
        if len(rows) > 4096: raise ValueError('月份历史超过读取上限')
        result, size = [], 0
        for row in rows:
            raw = row['payload']
            if raw is None: raise ValueError('月份记录超过大小上限')
            size += len(raw.encode('utf-8'))
            if size > 64 * 1024 * 1024: raise ValueError('月份历史超过大小上限')
            digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
            if digest != row['sha256']: raise ValueError('月份记录内容校验失败')
            payload = validate_monthly(json.loads(raw))
            if payload['date'] != row['date'] or payload['country'] != row['country']:
                raise ValueError('月份记录业务键不符')
            result.append(dict(payload=payload, version=digest, saved_at=row['saved_at']))
        return result

    def save_monthly(self, raw, *, expected_version):
        from .model import validate_monthly
        payload = validate_monthly(raw)
        text = packed(payload)
        if len(text.encode('utf-8')) > 262144: raise ValueError('月份记录超过大小上限')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self.write() as db:
            for statement in MONTHLY_DDL.split(';'):
                if statement.strip(): db.execute(statement)
            key = (payload['date'], payload['country'])
            old = db.execute('SELECT * FROM monthly_records WHERE date=? AND country=?', key).fetchone()
            if (old['sha256'] if old else None) != expected_version:
                raise ValueError('月份记录已被另一会话修改，请重新载入后保存')
            if old and old['payload'] == text: return digest
            if old:
                db.execute('INSERT INTO monthly_revisions(date,country,payload,sha256,saved_at) VALUES(?,?,?,?,?)',
                           (*key, old['payload'], old['sha256'], old['saved_at']))
            db.execute('INSERT INTO monthly_records VALUES(?,?,?,?,?) ON CONFLICT(date,country) DO UPDATE SET payload=excluded.payload,sha256=excluded.sha256,saved_at=excluded.saved_at',
                       (*key, text, digest, timestamp()))
        return digest

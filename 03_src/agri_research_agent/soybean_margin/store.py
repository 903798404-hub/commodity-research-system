"""Transactional CNF overrides in a new, separately configured database."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3

from .model import ORIGINS, number, shipment_year

DDL = """
CREATE TABLE IF NOT EXISTS cnf (
 business_date TEXT NOT NULL, origin TEXT NOT NULL, shipment_year INTEGER NOT NULL,
 shipment_month INTEGER NOT NULL, value REAL,
 PRIMARY KEY (business_date, origin, shipment_year, shipment_month));
CREATE TABLE IF NOT EXISTS revisions (
 business_date TEXT NOT NULL, origin TEXT NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY (business_date, origin));
"""


def load(path: Path, day: date, origin: str):
    if not path.is_file():
        return {}, 0
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT shipment_month,value,shipment_year FROM cnf WHERE business_date=? AND origin=?",
                          (day.isoformat(), origin)).fetchall()
        version = db.execute("SELECT version FROM revisions WHERE business_date=? AND origin=?",
                             (day.isoformat(), origin)).fetchone()
    if any(year != shipment_year(day, month) for month, _, year in rows):
        raise ValueError("CNF船期年份与业务日期不符")
    return {month: value for month, value, _ in rows}, version[0] if version else 0


def read_all(path: Path) -> list[dict]:
    """Read saved manual CNF for historical tables without creating a database."""
    if not path.is_file():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT business_date,origin,shipment_year,shipment_month,value FROM cnf"
        ).fetchall()
    records = []
    for day_text, origin, year, month, value in rows:
        day = date.fromisoformat(day_text)
        parsed = number(value)
        if (origin not in ORIGINS or day.weekday() >= 5
                or year != shipment_year(day, month)
                or (value is not None and parsed is None)):
            raise ValueError("已保存CNF的业务键或数值无效")
        records.append(dict(business_date=day, origin=origin, shipment_year=year,
            shipment_month=month, cnf_cents_per_bushel=parsed))
    return records


def save(path: Path, day: date, origin: str, values: dict, expected_version: int):
    if origin not in ORIGINS or type(day) is not date or day.weekday() >= 5:
        raise ValueError("产地或业务日期无效")
    normalized = {}
    for month, value in values.items():
        if type(month) is not int or not 1 <= month <= 12:
            raise ValueError("CNF月份无效")
        parsed = number(value)
        if value is not None and parsed is None:
            raise ValueError("CNF必须是有限数值或空值")
        normalized[month] = parsed
    if set(normalized) != set(range(1, 13)):
        raise ValueError("保存必须包含完整12个月")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.executescript(DDL)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT version FROM revisions WHERE business_date=? AND origin=?",
                         (day.isoformat(), origin)).fetchone()
        version = row[0] if row else 0
        if version != expected_version:
            raise ValueError("CNF已被另一会话修改，请重新读取后保存")
        for month, value in normalized.items():
            db.execute("INSERT INTO cnf VALUES (?,?,?,?,?) ON CONFLICT DO UPDATE SET value=excluded.value",
                       (day.isoformat(), origin, shipment_year(day, month), month, value))
        db.execute("INSERT INTO revisions VALUES (?,?,?) ON CONFLICT DO UPDATE SET version=excluded.version",
                   (day.isoformat(), origin, version + 1))
    return version + 1

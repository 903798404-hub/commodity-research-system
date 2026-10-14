"""Bounded Tushare requests with recursive truncation checks and raw evidence."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .model import FIELDS, PRODUCTS, ReceiptError


class TushareReceipts:
    def __init__(self, token: str, endpoint: str = "https://api.tushare.pro", *, pause: float = 0.35):
        parsed = urlsplit(endpoint)
        if not token or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
            raise ReceiptError("Tushare认证或HTTPS接口配置无效")
        self.token, self.endpoint, self.pause = token, endpoint, pause
        self.session = requests.Session()

    def query(self, symbol: str, start: dt.date, end: dt.date) -> dict:
        if symbol not in PRODUCTS or start > end:
            raise ReceiptError("仓单查询范围无效")
        body = dict(api_name="fut_wsr", token=self.token, fields=FIELDS,
                    params=dict(symbol=symbol, exchange=PRODUCTS[symbol][1],
                                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")))
        time.sleep(self.pause)
        try:
            response = self.session.post(self.endpoint, json=body, timeout=(10, 40))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise ReceiptError("仓单接口请求失败，未写入新快照") from None
        if payload.get("code") != 0:
            # Do not echo provider messages: gateways may include credentials there.
            raise ReceiptError(f"仓单接口拒绝请求（API code={payload.get('code')}）")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise ReceiptError("仓单响应结构无效")
        if not set(FIELDS.split(",")).issubset(data["fields"]) or len(set(data["fields"])) != len(data["fields"]):
            raise ReceiptError("仓单响应缺少完整源维度")
        if any(len(row) != len(data["fields"]) for row in data["items"]):
            raise ReceiptError("仓单响应字段长度不一致")
        rows = [dict(zip(data["fields"], row)) for row in data["items"]]
        if any(row["symbol"] != symbol or not start.strftime("%Y%m%d") <= str(row["trade_date"]) <= end.strftime("%Y%m%d") for row in rows):
            raise ReceiptError("仓单响应超出品种或日期范围")
        return dict(api_name="fut_wsr", endpoint=self.endpoint, params=body["params"], fields=data["fields"], rows=rows,
                    captured_at=dt.datetime.now(dt.timezone.utc).isoformat(), http_status=response.status_code)


def acquire_window(client: TushareReceipts, symbol: str, start: dt.date, end: dt.date,
                   raw_root: Path) -> list[dict]:
    """A full 1000-row response is retried as smaller disjoint windows."""
    raw_root.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-{start:%Y%m%d}-{end:%Y%m%d}"
    path = raw_root / f"{name}.json"
    digest_path = raw_root / f"{name}.sha256"
    if path.exists():
        clean = path.read_bytes()
        if not digest_path.exists() or hashlib.sha256(clean).hexdigest() != digest_path.read_text().strip():
            raise ReceiptError("已保存仓单源文件身份不匹配")
        record = json.loads(clean.decode("utf-8", errors="strict"))
        expected = dict(symbol=symbol, exchange=PRODUCTS[symbol][1], start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if record.get("api_name") != "fut_wsr" or record.get("endpoint") != client.endpoint or record.get("params") != expected:
            raise ReceiptError("已保存仓单查询身份不一致")
    else:
        record = client.query(symbol, start, end)
        clean = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
        path.write_bytes(clean)
        digest_path.write_text(hashlib.sha256(clean).hexdigest(), encoding="ascii")
    if len(record["rows"]) >= 1000:
        if start == end:
            raise ReceiptError("单日仓单触及返回上限，无法验证完整性")
        middle = start + (end - start) // 2
        return acquire_window(client, symbol, start, middle, raw_root) + acquire_window(client, symbol, middle + dt.timedelta(days=1), end, raw_root)
    return [record]


def acquire_history(client: TushareReceipts, start: dt.date, end: dt.date, raw_root: Path,
                    progress=lambda symbol, end, rows: None) -> list[dict]:
    records = []
    for symbol in PRODUCTS:
        cursor = start
        while cursor <= end:
            # Fourteen calendar days usually remain below 1000 warehouse rows.
            window_end = min(cursor + dt.timedelta(days=13), end)
            batch = acquire_window(client, symbol, cursor, window_end, raw_root)
            records.extend(batch)
            progress(symbol, window_end, sum(len(r["rows"]) for r in batch))
            cursor = window_end + dt.timedelta(days=1)
    return records

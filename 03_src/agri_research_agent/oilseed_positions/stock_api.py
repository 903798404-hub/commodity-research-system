"""User-selected Tushare-compatible service. Credentials never enter captures."""
from datetime import datetime, timezone
import json
import os
import re

import requests

from agri_research_agent.sugar_positions.model import integer, split_member, unique_rows
from .aggregation import aggregate_contract_rows
from .sources import SourceNotPublished

URL = 'https://t.xiaodefa.top/'
SCHEMA = 'stock-api-holdings/1'
MAX_ROWS = 2000
MAX_PAGES = 20
MAX_BYTES = 4 * 1024 * 1024


class StockApiError(ValueError):
    pass


def parse_capture(raw, source_url, retrieved_at):
    if source_url != URL or len(raw) > 32 * 1024 * 1024:
        raise StockApiError('stock_api_capture_identity_invalid')
    body = json.loads(raw.decode('utf-8'))
    if (set(body) != {'schema_version', 'trade_date', 'exchange', 'varieties', 'scope_contract', 'pages'}
            or body.get('schema_version') != SCHEMA or body.get('exchange') != 'DCE'):
        raise StockApiError('stock_api_capture_schema_invalid')
    day, varieties = body['trade_date'], body['varieties']
    if not re.fullmatch(r'\d{8}', day) or not varieties or set(varieties) - {'M', 'Y', 'P'}:
        raise StockApiError('stock_api_request_scope_invalid')
    report_date = datetime.strptime(day, '%Y%m%d').date().isoformat()
    scope_contract = body.get('scope_contract')
    if scope_contract is not None and not re.fullmatch(r'[MYP]\d{4}', scope_contract):
        raise StockApiError('stock_api_request_contract_invalid')
    records, seen = [], set()
    pages = body['pages']
    if not 1 <= len(pages) <= MAX_PAGES:
        raise StockApiError('stock_api_pagination_invalid')
    for i, payload in enumerate(pages):
        if set(payload) != {'fields', 'items'}:
            raise StockApiError('stock_api_data_schema_invalid')
        fields, items = payload['fields'], payload['items']
        required = {'trade_date', 'symbol', 'broker', 'long_hld', 'short_hld', 'long_chg', 'short_chg'}
        allowed = required | {'vol', 'vol_chg', 'exchange'}
        if (len(set(fields)) != len(fields) or not required <= set(fields)
                or set(fields) - allowed or len(items) > MAX_ROWS):
            raise StockApiError('stock_api_fields_or_rows_invalid')
        if (i < len(pages)-1 and len(items) != MAX_ROWS) or (i == len(pages)-1 and len(items) == MAX_ROWS):
            raise StockApiError('stock_api_pagination_incomplete')
        for values in items:
            if len(values) != len(fields):
                raise StockApiError('stock_api_row_schema_invalid')
            record = dict(zip(fields, values))
            if record['trade_date'] != day:
                raise StockApiError('stock_api_report_date_mismatch')
            scope, raw_member = record['symbol'], record['broker']
            if not isinstance(scope, str) or not re.fullmatch(r'[A-Z]+\d{4}', scope):
                raise StockApiError('stock_api_exact_contract_required')
            if scope_contract is not None and scope != scope_contract:
                raise StockApiError('stock_api_contract_mismatch')
            if not isinstance(raw_member, str) or not raw_member.strip():
                raise StockApiError('stock_api_member_invalid')
            key = scope, raw_member
            if key in seen:
                raise StockApiError('stock_api_duplicate_or_repeated_page')
            seen.add(key)
            if scope[:-4] not in varieties:
                continue
            member, account = split_member(raw_member)
            for side, amount, change in (('long', 'long_hld', 'long_chg'), ('short', 'short_hld', 'short_chg')):
                if record[amount] is None:
                    continue  # Missing disclosure is never zero.
                if isinstance(record[amount], bool):
                    raise StockApiError('stock_api_position_invalid')
                records.append(dict(report_date=report_date, scope=scope, side=side,
                    member=member, raw_member=raw_member, account=account,
                    positions=integer(record[amount]),
                    reported_change=integer(record[change], signed=True, optional=True),
                    source_provider='stock_api', source_url=URL, retrieved_at=retrieved_at,
                    unit='contracts'))
    if not records:
        raise SourceNotPublished('stock_api_report_not_published')
    unique_rows(records, ('scope', 'side', 'member', 'account'))
    for contract in sorted({r['scope'] for r in records}):
        for side in ('long', 'short'):
            selected = [r for r in records if r['scope'] == contract and r['side'] == side]
            if not selected or len(selected) > 20:
                raise StockApiError('stock_api_side_missing_or_overfull')
            selected.sort(key=lambda r: (-r['positions'], r['member'], r['account']))
            for rank, record in enumerate(selected, 1):
                record['rank'] = rank
    # Source lists all disclosed exact-contract reports for the requested day.
    # Re-rank member sums; never label this as an official variety top-20.
    if scope_contract is not None:
        return records
    summaries = []
    for variety in varieties:
        selected = [r for r in records if r['scope'].startswith(variety)]
        contracts = sorted({r['scope'] for r in selected})
        if not contracts:
            raise SourceNotPublished('stock_api_variety_not_published')
        summaries.extend(aggregate_contract_rows(selected, contracts))
    return records + summaries


class StockApiSources:
    def __init__(self, *, token=None, session=None, timeout=20):
        self.token = token if token is not None else os.getenv('STOCK_API_TOKEN', '').strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        if not self.token or len(self.token) > 512:
            raise StockApiError('stock_api_credential_missing_or_invalid')

    def holdings(self, day, varieties, *, contract=None):
        pages = []
        try:
            for index in range(MAX_PAGES):
                params = dict(trade_date=day.strftime('%Y%m%d'), exchange='DCE',
                              limit=MAX_ROWS, offset=index*MAX_ROWS)
                if contract is not None:
                    params['symbol'] = contract
                # The gateway requires gzip. Authentication is body-only, not in the URL.
                with self.session.post(URL, json=dict(api_name='fut_holding', token=self.token,
                        params=params), headers={'Accept-Encoding': 'gzip'},
                        timeout=self.timeout, stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise StockApiError('stock_api_http_error')
                    blocks, size = [], 0
                    for block in response.iter_content(16384):
                        size += len(block)
                        if size > MAX_BYTES:
                            raise StockApiError('stock_api_response_too_large')
                        blocks.append(block)
                    payload = json.loads(b''.join(blocks).decode('utf-8'))
                if payload.get('code') != 0:
                    raise StockApiError('stock_api_request_rejected')
                data = payload['data']
                # Store only the closed data shape; arbitrary messages are not persisted.
                pages.append(dict(fields=data['fields'], items=data['items']))
                if len(data['items']) < MAX_ROWS:
                    break
            raw = json.dumps(dict(schema_version=SCHEMA, trade_date=day.strftime('%Y%m%d'),
                exchange='DCE', varieties=list(varieties), scope_contract=contract, pages=pages),
                ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            rows = parse_capture(raw, URL, datetime.now(timezone.utc).isoformat())
            return rows, raw, URL
        except (StockApiError, SourceNotPublished):
            raise
        except Exception:
            raise StockApiError('stock_api_transport_or_schema_failed') from None

"""User-selected Tushare-compatible service. Credentials never enter captures."""
from datetime import datetime, timezone, timedelta
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
COFCO_HISTORY_SCHEMA = 'stock-api-cofco-history/1'
COFCO_HISTORY_RECORD = 'cofco_contract_history_v1'


class StockApiError(ValueError):
    pass


def parse_cofco_history(raw, source_url, retrieved_at):
    """Replay a complete SR catalogue and paginated contract responses.

    These are member observations, including explicit absence markers, rather
    than exchange rankings. Original account labels and source zeros survive.
    """
    from collections import defaultdict
    from agri_research_agent.positions.delivery import strict_json, exact, require
    from agri_research_agent.sugar_positions.model import COFCO_MEMBER
    require(source_url == URL and 0 < len(raw) <= 32 * 1024**2, 'COFCO source identity invalid')
    body = strict_json(raw)
    exact(body, ('schema_version', 'start_date', 'end_date', 'catalogue', 'calendar', 'reports'), 'COFCO capture')
    require(body['schema_version'] == COFCO_HISTORY_SCHEMA, 'COFCO capture version invalid')
    start, end = body['start_date'], body['end_date']
    for value in (start, end):
        require(type(value) is str and re.fullmatch(r'20\d{6}', value), 'COFCO request date invalid')
        datetime.strptime(value, '%Y%m%d')
    require(start <= end <= datetime.now(timezone.utc).strftime('%Y%m%d'), 'COFCO request range invalid')
    catalogue = exact(body['catalogue'], ('fields', 'items'), 'COFCO catalogue')
    fields, items = catalogue['fields'], catalogue['items']
    needed = {'ts_code', 'symbol', 'exchange', 'fut_code', 'list_date', 'delist_date'}
    allowed = needed | {'name', 'multiplier', 'trade_unit', 'per_unit', 'quote_unit',
        'quote_unit_desc', 'd_mode_desc', 'd_month', 'last_ddate', 'trade_time_desc'}
    require(type(fields) is list and len(set(fields)) == len(fields) and needed <= set(fields) and not set(fields) - allowed
            and type(items) is list and 0 < len(items) < 10000, 'COFCO catalogue incomplete')
    expected, seen, listings = set(), set(), {}
    for values in items:
        require(type(values) is list and len(values) == len(fields), 'COFCO catalogue row invalid')
        row = dict(zip(fields, values))
        code = row['ts_code']
        require(type(code) is str and re.fullmatch(r'SR\d{4}\.ZCE', code)
                and row['exchange'] == 'CZCE' and row['fut_code'] == 'SR'
                and code not in seen, 'COFCO catalogue identity invalid')
        seen.add(code)
        for field in ('list_date', 'delist_date'):
            require(type(row[field]) is str and re.fullmatch(r'\d{8}', row[field]), 'COFCO listing date invalid')
            datetime.strptime(row[field], '%Y%m%d')
        if row['list_date'] <= end and row['delist_date'] >= start:
            expected.add(code.split('.')[0])
            listings[code.split('.')[0]] = (row['list_date'], row['delist_date'])
    calendar = exact(body['calendar'], ('fields', 'items'), 'COFCO calendar')
    fields, items = calendar['fields'], calendar['items']
    require(type(fields) is list and len(set(fields)) == len(fields)
            and {'exchange', 'cal_date', 'is_open'} <= set(fields)
            and not set(fields) - {'exchange', 'cal_date', 'is_open', 'pretrade_date'}
            and type(items) is list and 0 < len(items) < 2000, 'COFCO calendar invalid')
    calendar_days, open_days = set(), set()
    for values in items:
        require(type(values) is list and len(values) == len(fields), 'COFCO calendar row invalid')
        row = dict(zip(fields, values))
        day = row['cal_date']
        require(type(day) is str and re.fullmatch(r'\d{8}', day) and start <= day <= end
                and row['exchange'] == 'CZCE' and str(row['is_open']) in {'0', '1'}
                and day not in calendar_days, 'COFCO calendar identity invalid')
        datetime.strptime(day, '%Y%m%d')
        calendar_days.add(day)
        if str(row['is_open']) == '1':
            open_days.add(day)
    reports = body['reports']
    require(type(reports) is list and 0 < len(reports) <= 120, 'COFCO report count invalid')
    require(len(reports) == len(expected) and {r['symbol'] for r in reports} == expected,
            'COFCO contract catalogue coverage incomplete')
    result, report_days = [], set()
    for report in reports:
        exact(report, ('symbol', 'windows'), 'COFCO report')
        symbol, windows = report['symbol'], report['windows']
        require(type(windows) is list and 1 <= len(windows) <= 120, 'COFCO windows invalid')
        days, keys = defaultdict(list), set()
        cursor = start
        for window in windows:
            exact(window, ('start_date', 'end_date', 'pages'), 'COFCO window')
            first, last, pages = window['start_date'], window['end_date'], window['pages']
            require(first == cursor and first <= last <= end, 'COFCO date window coverage incomplete')
            cursor = (datetime.strptime(last, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
            require(type(pages) is list and 1 <= len(pages) <= MAX_PAGES, 'COFCO pages invalid')
            for index, page in enumerate(pages):
                exact(page, ('fields', 'items'), 'COFCO page')
                fields, items = page['fields'], page['items']
                required = {'trade_date', 'symbol', 'broker', 'long_hld', 'short_hld', 'long_chg', 'short_chg'}
                require(type(fields) is list and len(set(fields)) == len(fields) and required <= set(fields)
                        and not set(fields) - (required | {'vol', 'vol_chg', 'exchange'})
                        and type(items) is list and len(items) <= MAX_ROWS, 'COFCO page fields invalid')
                require((len(items) == MAX_ROWS) == (index < len(pages) - 1), 'COFCO pagination incomplete')
                for values in items:
                    require(type(values) is list and len(values) == len(fields), 'COFCO holding row invalid')
                    row = dict(zip(fields, values))
                    day, broker = row['trade_date'], row['broker']
                    require(type(day) is str and re.fullmatch(r'\d{8}', day) and first <= day <= last
                            and row['symbol'] == symbol, 'COFCO holding date or contract differs')
                    datetime.strptime(day, '%Y%m%d')
                    require(type(broker) is str and broker.strip() and (day, broker) not in keys,
                            'COFCO duplicate member or repeated page')
                    if 'exchange' in row:
                        require(row['exchange'] == 'CZCE', 'COFCO exchange differs')
                    keys.add((day, broker))
                    for field in ('long_hld', 'short_hld', 'vol'):
                        if field in row:
                            require(not isinstance(row[field], bool), 'COFCO position invalid')
                            integer(row[field], optional=True)
                    for field in ('long_chg', 'short_chg', 'vol_chg'):
                        if field in row:
                            require(not isinstance(row[field], bool), 'COFCO change invalid')
                            integer(row[field], signed=True, optional=True)
                    days[day].append(row)
        require(cursor == (datetime.strptime(end, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d'), 'COFCO date window coverage incomplete')
        for day, values in sorted(days.items()):
            report_days.add(day)
            selected = [r for r in values if split_member(r['broker'])[0] in COFCO_MEMBER['aliases']]
            # An absent member is an explicit blank, never an inferred zero or rank.
            for row in selected or [None]:
                _, account = split_member(row['broker']) if row else ('中粮期货', '未披露')
                for side in ('long', 'short'):
                    result.append(dict(record_type=COFCO_HISTORY_RECORD,
                        report_date=datetime.strptime(day, '%Y%m%d').date().isoformat(), scope=symbol,
                        side=side, member=COFCO_MEMBER['label'], raw_member=row['broker'] if row else None, account=account,
                        positions=integer(row[side + '_hld'], optional=True) if row else None,
                        reported_change=integer(row[side + '_chg'], signed=True, optional=True) if row else None,
                        source_provider='stock_api', source_url=URL, retrieved_at=retrieved_at, unit='contracts',
                        disclosure='source_value' if row else 'member_absent'))
    # A known open day with no returned SR report is an explicit chart gap.
    # Unreturned calendar dates remain unknown; they are not assumed closed.
    for day in sorted(open_days - report_days):
        for symbol, (listed, delisted) in sorted(listings.items()):
            if not listed <= day <= delisted:
                continue
            for side in ('long', 'short'):
                result.append(dict(record_type=COFCO_HISTORY_RECORD,
                    report_date=datetime.strptime(day, '%Y%m%d').date().isoformat(), scope=symbol,
                    side=side, member=COFCO_MEMBER['label'], raw_member=None, account='未披露',
                    positions=None, reported_change=None, source_provider='stock_api', source_url=URL,
                    retrieved_at=retrieved_at, unit='contracts', disclosure='report_absent'))
    unique_rows(result, ('report_date', 'scope', 'side', 'member', 'account'))
    require(bool(result), 'COFCO history empty')
    return result


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

    def _history_data(self, api, params):
        with self.session.post(URL, json=dict(api_name=api, token=self.token, params=params),
                headers={'Accept-Encoding': 'gzip'}, timeout=self.timeout, stream=True,
                allow_redirects=False) as response:
            if response.status_code != 200:
                raise StockApiError('stock_api_http_error')
            raw = bytearray()
            for block in response.iter_content(16384):
                raw.extend(block)
                if len(raw) > MAX_BYTES:
                    raise StockApiError('stock_api_response_too_large')
        payload = json.loads(raw.decode('utf-8'))
        if payload.get('code') != 0:
            raise StockApiError('stock_api_request_rejected')
        data = payload['data']
        return dict(fields=data['fields'], items=data['items'])

    def cofco_history(self, day):
        """A single trading day's exact-contract member observations, never SR totals."""
        try:
            compact = day.strftime('%Y%m%d')
            if not hasattr(self, '_cofco_catalogue'):
                self._cofco_catalogue = self._history_data('fut_basic',
                    dict(exchange='CZCE', fut_type='1', fut_code='SR', limit=10000))
            catalogue = self._cofco_catalogue
            if not 0 < len(catalogue['items']) < 10000:
                raise StockApiError('stock_api_cofco_catalogue_incomplete')
            selected = []
            for values in catalogue['items']:
                row = dict(zip(catalogue['fields'], values))
                if row['list_date'] <= compact <= row['delist_date']:
                    if not re.fullmatch(r'SR\d{4}\.ZCE', row['ts_code']):
                        raise StockApiError('stock_api_cofco_contract_invalid')
                    selected.append(row['ts_code'].split('.')[0])
            calendar = self._history_data('fut_trade_cal',
                dict(exchange='CZCE', start_date=compact, end_date=compact, limit=2000, offset=0))
            dates = [dict(zip(calendar['fields'], row)) for row in calendar['items']]
            if len(dates) != 1 or dates[0]['cal_date'] != compact or dates[0]['exchange'] != 'CZCE':
                raise StockApiError('stock_api_cofco_calendar_invalid')
            if str(dates[0]['is_open']) == '0':
                raise SourceNotPublished('stock_api_cofco_nontrading_day')
            reports, count = [], 0
            for symbol in sorted(selected):
                pages = []
                for index in range(MAX_PAGES):
                    data = self._history_data('fut_holding', dict(exchange='CZCE', symbol=symbol,
                        start_date=compact, end_date=compact, limit=MAX_ROWS, offset=index * MAX_ROWS))
                    pages.append(data)
                    count += len(data['items'])
                    if len(data['items']) < MAX_ROWS:
                        break
                reports.append(dict(symbol=symbol, windows=[dict(start_date=compact, end_date=compact, pages=pages)]))
            if not count:
                # A not-yet-published day must remain retryable, not become a saved blank partition.
                raise SourceNotPublished('stock_api_cofco_report_not_published')
            raw = json.dumps(dict(schema_version=COFCO_HISTORY_SCHEMA, start_date=compact, end_date=compact,
                catalogue=catalogue, calendar=calendar, reports=reports), ensure_ascii=False,
                allow_nan=False, separators=(',', ':')).encode('utf-8')
            rows = parse_cofco_history(raw, URL, datetime.now(timezone.utc).isoformat())
            return rows, raw, URL
        except (StockApiError, SourceNotPublished):
            raise
        except Exception:
            raise StockApiError('stock_api_cofco_transport_or_schema_failed') from None

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

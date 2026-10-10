"""Bounded, timestamped latest prices. No schedules or daily-history writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
import json
import re

import requests

from .api_sources import SHANGHAI, SourceError, MONTH_CODES, fx_curve, _worker
from .model import contracts, number

EM_URL = 'https://futsseapi.eastmoney.com/list/COMEX,NYMEX,COBOT,SGX,NYBOT,LME,MDEX,TOCOM,IPE'
SINA_URL = 'https://hq.sinajs.cn/'
MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20


def stamp():
    return datetime.now(SHANGHAI)


def response_bytes(response, limit=MAX_BYTES):
    response.raise_for_status()
    chunks, size = [], 0
    for block in response.iter_content(16384):
        size += len(block)
        if size > limit:
            raise SourceError('response_too_large')
        chunks.append(block)
    return b''.join(chunks)


def domestic(symbols, *, session=requests, now=None, max_age_seconds=3600):
    now = now or stamp()
    if not symbols or any(not re.fullmatch(r'[MY]\d{4}', s) for s in symbols):
        raise SourceError('domestic_contract_invalid')
    # Use Sina's upstream directly; preserves appended fields and the omitted date.
    with session.get(SINA_URL + '?list=' + ','.join('nf_' + s for s in symbols),
                     headers={'Referer': 'https://vip.stock.finance.sina.com.cn/'},
                     timeout=(5, 10), stream=True) as response:
        raw = response_bytes(response, 65536)
    matches = re.findall(r'var hq_str_nf_([A-Z]+\d{4})="([^"\r\n]*)";', raw.decode('gb18030'))
    if len({s for s, _ in matches}) != len(matches) or any(s not in symbols for s, _ in matches):
        raise SourceError('domestic_identity_invalid')
    records = dict(matches)
    prices, evidence = dict.fromkeys(symbols), {}
    for symbol in symbols:
        fields = records.get(symbol, '').split(',')
        item = dict(provider='Sina', contract=symbol, unit='CNY/tonne', price=None,
                    quoted_at=None, status='not_available', raw_fields=fields)
        if len(fields) >= 28:
            try:
                quoted = datetime.strptime(fields[17] + ' ' + fields[1].zfill(6),
                                           '%Y-%m-%d %H%M%S').replace(tzinfo=SHANGHAI)
                age = (now - quoted).total_seconds()
                item['quoted_at'] = quoted.isoformat()
                price = number(fields[8])
                if price is None or price <= 0:
                    item['status'] = 'price_missing'
                elif quoted.date() != now.date() or not 0 <= age <= max_age_seconds:
                    item['status'] = 'stale_or_future'
                else:
                    item.update(price=price, status='available')
                    prices[symbol] = price
            except ValueError:
                item['status'] = 'timestamp_invalid'
        evidence[symbol] = item
    return prices, dict(provider='Sina', quotes=evidence,
                        raw_sha256=hashlib.sha256(raw).hexdigest())


def eastmoney(codes, *, session=requests):
    if (not codes or len(codes) != len(set(codes)) or any(
            not re.fullmatch(r'\d{4}', c) or int(c[2:]) not in MONTH_CODES for c in codes)):
        raise SourceError('cbot_contract_invalid')
    symbols = {f'ZS{code[:2]}{MONTH_CODES[int(code[2:])]}': code for code in codes}
    rows, pages, page_size, total = [], [], 1000, None
    for index in range(MAX_PAGES):
        params = dict(orderBy='dm', sort='desc', pageSize=page_size, pageIndex=index,
                      token='58b2fa8f54638b60b87d69b31969089c')  # Public site's query constant.
        with session.get(EM_URL, params=params, headers={'Referer': 'https://quote.eastmoney.com/'},
                         timeout=(5, 10), stream=True) as response:
            raw = response_bytes(response)
        body = json.loads(raw.decode('utf-8'))
        records = body['list']
        if (type(body['total']) is not int or not 0 <= body['total'] <= 10000
                or not isinstance(records, list) or not records):
            raise SourceError('eastmoney_list_invalid')
        if total is None:
            total = body['total']
            page_size = len(records)  # Upstream may cap the requested page size.
        if body['total'] != total or (index and len(records) > page_size):
            raise SourceError('eastmoney_pagination_changed')
        rows.extend(records)
        pages.append(dict(page=index, raw_sha256=hashlib.sha256(raw).hexdigest()))
        if len(rows) >= total:
            break
    if len(rows) != total or len({r['dm'] for r in rows}) != total:
        raise SourceError('eastmoney_list_incomplete_or_duplicate')
    selected = {r['dm']: r for r in rows if r['dm'] in symbols}
    prices, quotes = dict.fromkeys(codes), {}
    for symbol, code in symbols.items():
        record = selected.get(symbol)
        price = number(record.get('p')) if record else None
        price = price if price is not None and price > 0 else None
        prices[code] = price
        quotes[code] = dict(provider='Eastmoney', contract=symbol, price=price,
            unit='US_cents/bushel', quoted_at=None,
            status='timestamp_unknown' if price is not None else 'price_missing',
            timestamp_semantics='unverified_provider_fields', raw_record=record)
    return prices, dict(provider='Eastmoney', quotes=quotes, pages=pages, total=total)


def capture(day, *, now=None, calendar=None, providers=None):
    now = now or stamp()
    if now.utcoffset() is None or day != now.astimezone(SHANGHAI).date():
        raise SourceError('current_business_date_required')
    calendar = calendar or (lambda: _worker('calendar', 35))
    days = {date.fromisoformat(d) for d in calendar()}
    if not days or day > max(days):
        raise SourceError('trading_calendar_out_of_range')
    if day not in days:
        raise SourceError('domestic_market_holiday')
    codes = sorted({contracts(day, m)[0] for m in range(1, 13)})
    symbols = sorted({p + contracts(day, m)[1] for p in ('M', 'Y') for m in range(1, 13)})
    providers = providers or dict(cbot=lambda: eastmoney(codes),
        domestic=lambda: domestic(symbols), fx=lambda: fx_curve(stamp()))
    result = dict(business_date=day.isoformat(), cbot=dict.fromkeys(codes),
                  domestic=dict.fromkeys(symbols), fx_curve={}, sources={}, errors={})

    def run(name, provider):
        requested = stamp().isoformat()
        try:
            values, evidence = provider()
            return name, values, dict(evidence, requested_at=requested,
                                     returned_at=stamp().isoformat()), None
        except Exception as exc:
            # Never put an upstream exception (possibly containing credentials) in records.
            error = str(exc) if isinstance(exc, SourceError) else 'transport_or_schema_error'
            return name, None, dict(requested_at=requested, returned_at=stamp().isoformat()), error

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(run, name, provider) for name, provider in providers.items()]
        for future in futures:
            name, values, evidence, error = future.result()
            result['sources'][name] = evidence
            if error:
                result['errors'][name] = error
            else:
                result['fx_curve' if name == 'fx' else name] = values
    result['captured_at'] = stamp().isoformat()
    if datetime.fromisoformat(result['captured_at']).date() != day:
        raise SourceError('collection_crossed_business_date')
    return result

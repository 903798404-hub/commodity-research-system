"""Official disaggregated annual files, preserving published API report partitions."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import io
import re
import zipfile

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from agri_research_agent.sugar_positions.sources import CFTC_FIELDS, parse_cftc


def archive_url(year, kind):
    if type(year) is not int or not 2009 <= year <= datetime.now(timezone.utc).year or kind not in {'futures_only','combined'}:
        raise ValueError('CFTC annual identity invalid')
    prefix='fut' if kind=='futures_only' else 'com'
    return f'https://www.cftc.gov/files/dea/history/{prefix}_disagg_txt_{year}.zip'


def parse_archive(raw,year,kind,market,code,name,stamp):
    url=archive_url(year,kind)
    if not 0 < len(raw) <= 16*1024**2:raise ValueError('CFTC archive size invalid')
    with zipfile.ZipFile(io.BytesIO(raw)) as packed:
        entries=packed.infolist()
        if (len(entries)!=1 or not re.fullmatch(r'[A-Za-z0-9_.-]+\.txt',entries[0].filename)
            or not 0 < entries[0].file_size <= 64*1024**2 or entries[0].flag_bits & 1):
            raise ValueError('CFTC archive expansion invalid')
        text=packed.read(entries[0]).decode('utf-8-sig',errors='strict')
    reader=csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):
        raise ValueError('CFTC CSV header invalid')
    selected=[]
    aliases={'report_date_as_yyyy_mm_dd':'report_date_as_yyyy-mm-dd',
        'prod_merc_positions_long':'prod_merc_positions_long_all','prod_merc_positions_short':'prod_merc_positions_short_all',
        'm_money_positions_spread':'m_money_positions_spread_all',
        'other_rept_positions_long':'other_rept_positions_long_all','other_rept_positions_short':'other_rept_positions_short_all',
        'other_rept_positions_spread':'other_rept_positions_spread_all',
        'tot_rept_positions_short':'tot_rept_positions_short_all'}
    required={'report_date_as_yyyy_mm_dd','cftc_contract_market_code','market_and_exchange_names','open_interest_all',
              'tot_rept_positions_long_all','tot_rept_positions_short',*(f for fields in CFTC_FIELDS.values() for f in fields if f)}
    count=0
    for record in reader:
        count+=1
        if count>100000:raise ValueError('CFTC annual row bound exceeded')
        value={key.lower():item.strip() for key,item in record.items() if key is not None and item is not None}
        if value.get('cftc_contract_market_code')!=code:continue
        if value.get('futonly_or_combined')!=('FutOnly' if kind=='futures_only' else 'Combined'):
            raise ValueError('CFTC archive report type differs')
        normalized={field:value[aliases.get(field,field)] for field in required}
        if date.fromisoformat(normalized['report_date_as_yyyy_mm_dd']).year!=year:
            raise ValueError('CFTC archive year differs')
        selected.append(normalized)
    return parse_cftc(selected,kind,url,stamp,market=market,expected_code=code,expected_name=name)


def unseen_rows(rows,previous,market,kind,start_year):
    """Compare report values; retain the original source attribution for old rows."""
    def group(values):
        result=defaultdict(list)
        for row in values:
            if row['market']==market and row['report_type']==kind and int(row['report_date'][:4])>=start_year:
                result[row['report_date']].append(row)
        return result
    new,old=group(rows),group(previous)
    if set(old)-set(new):raise ValueError('CFTC archive drops existing reporting dates')
    clean=lambda values:sorted(({k:v for k,v in row.items() if k not in {'source_url','retrieved_at'}} for row in values),key=lambda r:r['group'])
    for day,values in old.items():
        if clean(values)!=clean(new[day]):raise ValueError('CFTC historical revision requires review')
    return [row for row in rows if row['report_date'] not in old]


class AnnualSources:
    def __init__(self,today):
        self.today=today;self.cache={};self.session=requests.Session();self.session.trust_env=False
        self.session.mount('https://',HTTPAdapter(max_retries=Retry(total=2,backoff_factor=1,
            status_forcelist=(429,502,503,504),allowed_methods={'GET'})))

    def fetch(self,year,kind):
        url=archive_url(year,kind)
        if url not in self.cache:
            with self.session.get(url,timeout=(15,60),stream=True,allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code!=200:raise ValueError('CFTC archive response invalid')
                raw=bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw)>16*1024**2:raise ValueError('CFTC archive too large')
            self.cache[url]=bytes(raw)
        return self.cache[url],url

    def collect(self,domain,spec,previous,start_year):
        if type(start_year) is not int or not 2009 <= start_year <= self.today.year:
            raise ValueError('CFTC annual year range invalid')
        # Re-fetch the current and previous year; retain older verified captures and rows.
        start_year=max(start_year,self.today.year-1)
        markets={'sugar11':{'code':'080732','name':'SUGAR NO. 11'}} if domain=='sugar' else {
            m:s for m,s in spec['foreign'].items() if s['source']=='cftc'}
        result,captures,attempts=[],[],[]
        stamp=datetime.now(timezone.utc).isoformat()
        for market,definition in markets.items():
            for kind in ('futures_only','combined'):
                rows=[];owned=[]
                try:
                    for year in range(start_year,self.today.year+1):
                        raw,url=self.fetch(year,kind)
                        parsed=parse_archive(raw,year,kind,market,definition['code'],definition['name'],stamp)
                        rows.extend(parsed)
                        owned.append((f'cftc_archive_{year}_{market}_{kind}',raw,url,'zip'))
                    fresh=unseen_rows(rows,previous,market,kind,start_year)
                    result.extend(fresh);captures.extend(owned)
                    attempts.append({'source_id':f'cftc_annual_{market}_{kind}','status':'ok','rows':len(rows),'new_rows':len(fresh)})
                except (requests.RequestException,ValueError,KeyError,TypeError,zipfile.BadZipFile) as exc:
                    attempts.append({'source_id':f'cftc_annual_{market}_{kind}','status':'failed','error_type':type(exc).__name__})
        return result,captures,attempts

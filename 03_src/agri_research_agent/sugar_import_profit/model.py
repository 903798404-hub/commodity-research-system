"""Preserve the accepted standalone model and quote-date validation."""
from datetime import date, datetime, timedelta, timezone
import math

COUNTRIES = ('巴西', '泰国')
MODEL_VERSION = 'sugar-import-v1'
TZ = timezone(timedelta(hours=8))
NUMERIC = ('ice', 'premium', 'polarization', 'conversion', 'freight', 'insurance_fee', 'fx',
           'tariff_in', 'tariff_out', 'vat', 'loss', 'processing', 'other', 'domestic')
DATE_FIELDS = ('date', 'ice_date', 'premium_date', 'freight_date', 'fx_date', 'domestic_date')
LABELS = dict(ice='原糖结算价', premium='升贴水', polarization='旋光度增值系数', conversion='单位换算系数',
              freight='海运费', insurance_fee='保险费', fx='在岸人民币汇率', tariff_in='配额内关税',
              tariff_out='配额外关税', vat='增值税', loss='损耗率/加成率', processing='加工费', other='其他费用',
              domestic='日照糖价', date='计算日期', ice_date='原糖报价日', premium_date='升贴水报价日',
              freight_date='海运费报价日', fx_date='汇率报价日', domestic_date='日照糖价报价日')

def today():
    return datetime.now(TZ).date().isoformat()

def timestamp():
    return datetime.now(TZ).isoformat(timespec='seconds')

def calculate(d):
    fob = (d['ice'] + d['premium']) * d['polarization'] * d['conversion']
    cif_usd = fob + d['freight'] + d['insurance_fee']
    cif_cny = cif_usd * d['fx']
    result = dict(fob=fob, insurance_fee=d['insurance_fee'], cif_usd=cif_usd, cif_cny=cif_cny,
                  insurance_rate=d['insurance_fee']/(fob+d['freight']))
    for quota in ('in', 'out'):
        taxed = cif_cny*(1+d['tariff_'+quota])*(1+d['vat'])
        processed = (taxed/(1-d['loss']) if d['loss_mode']=='实际损耗' else taxed*(1+d['loss']))
        cost = processed+d['processing']+d['other']
        result.update({f'taxed_{quota}':taxed, f'cost_{quota}':cost, f'profit_{quota}':d['domestic']-cost})
    return result

def validate_inputs(raw):
    clean = {}; errors = []
    for key in NUMERIC:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append(LABELS[key]+'未填写有效数值')
        else:
            clean[key] = float(value)
    for key in DATE_FIELDS:
        try:
            clean[key] = date.fromisoformat(raw.get(key, '')).isoformat()
        except (ValueError, TypeError):
            errors.append(LABELS[key]+'未填写日期')
    for key,label in [('contract','原糖合约'),('source_note','价格来源')]:
        value = raw.get(key)
        if not isinstance(value,str) or not value.strip():
            errors.append(label+'未填写')
        elif len(value)>4000:
            errors.append(label+'过长')
        else:clean[key]=value.strip()
    clean['loss_mode'] = raw.get('loss_mode')
    if clean['loss_mode'] not in ('成本加成','实际损耗'):errors.append('损耗口径无效')
    if raw.get('example'):errors.append('中哲示例仅用于试算，不能保存为实际每日记录')
    if not errors:
        for key in ('ice','polarization','conversion','fx'):
            if clean[key]<=0:errors.append(LABELS[key]+'必须大于0')
        for key in ('freight','insurance_fee','processing','other','domestic'):
            if clean[key]<0:errors.append(LABELS[key]+'不能为负数')
        for key in ('tariff_in','tariff_out','vat','loss'):
            if not 0<=clean[key]<1:errors.append(LABELS[key]+'应在0%至100%之间')
        if clean['ice']+clean['premium']<=0:errors.append('原糖价与升贴水之和必须大于0')
        if clean['date']>today():errors.append('计算日期不能晚于今天')
        for key in DATE_FIELDS[1:]:
            if clean[key]>clean['date']:errors.append(LABELS[key]+'晚于计算日期')
    if errors:raise ValueError('；'.join(errors))
    return clean


# Monthly records deliberately have a separate schema from the original spot model.
MONTHLY_VERSION = 'sugar-monthly/1'
QUOTE_MODES = ('升贴水（美分/磅，不含运费）', 'CNF升贴水（美分/磅，含运费）', 'CNF全价（美元/吨）')
MONTH_CODES = {3: 'H', 5: 'K', 7: 'N', 10: 'V'}
MONTHLY_PARAMETERS = dict(polarization=1.042, conversion=22.046, tariff_in=.15,
    tariff_out=.50, vat=.13, loss=.04, loss_mode='成本加成', processing=400., other=0.)
RESEARCH_VERSION = 'sugar-monthly/2'
RESEARCH_PARAMETERS = dict(polarization=1.04, conversion=22.046, tariff_in=.15,
    tariff_out=.50, vat=.13, loss=.02, loss_mode='成本加成', processing=500., other=0.,
    insurance_rate=.004616, bank_rate=.00125, agency_rate=.01, interest=25.)
LABELS.update(insurance_rate='海运保险费率', bank_rate='银行手续费率',
              agency_rate='外贸代理费率', interest='利息及劳务费')


def month_periods(day):
    """Same future-twelve-month convention as the palm page; current month is next year."""
    return [f'{day.year + (month <= day.month)}-{month:02}' for month in range(1, 13)]


def default_mapping(period):
    year, month = map(int, period.split('-'))
    ice_month = next((m for m in MONTH_CODES if m > month), 3)
    ice_year = year + (ice_month <= month)
    # Explicit research assumption: two months transit/refining, then next SR 1/5/9.
    arrival = year * 12 + month - 1 + 2
    arrival_year, arrival_month = divmod(arrival, 12)
    arrival_month += 1
    sr_month = next((m for m in (1, 5, 9) if m >= arrival_month), 1)
    sr_year = arrival_year + (sr_month < arrival_month)
    return f'SB{MONTH_CODES[ice_month]}{ice_year % 100:02}', f'SR{sr_year % 100:02}{sr_month:02}'


def ice_quote(market, code):
    """Old readers retain the original nullable latest quote; closes are additive evidence."""
    latest = market.get('ice', {}).get(code) or {}
    return latest if latest.get('price') is not None else market.get('ice_closes', {}).get(code) or latest


def monthly_rows(payload):
    """Partial calculation: actual margins require insurance; estimates name the omission."""
    market, params = payload.get('market') or {}, payload['parameters']
    historical = payload.get('historical_market')
    if historical is not None:
        if payload.get('market') is not None: raise ValueError('历史补录与实时快照不能混用')
        from .runtime import historical_market
        market = historical_market(historical, date.fromisoformat(payload['date']))
    research = payload['schema_version'] == RESEARCH_VERSION
    rows = []
    for item in payload['quotes']:
        r = dict(item)
        month = int(r['period'][-2:])
        year = int(r['period'][:4])
        day = date.fromisoformat(payload['date'])
        tenor = (year - day.year) * 12 + month - day.month
        from agri_research_agent.commodity_import_margin.model import select_fx
        fx = market.get('fx_spot') if historical is not None else select_fx(market.get('fx_curve', {}), tenor)[0]
        ice_item = ice_quote(market, r['ice_contract'])
        ice = ice_item.get('price')
        r['ice_price_kind'] = ('收盘价' if ice_item.get('time_kind') == 'daily_close' else '最新价') if ice is not None else ''
        r['ice_quote_date'] = (ice_item.get('quote_date') or ice_item.get('quoted_at') or '')[:10]
        sr = (market.get('domestic', {}).get(r['sr_contract']) or {}).get('price')
        domestic = market.get('domestic', {}).get(r['sr_contract']) or {}
        r.update(fx_kind=market.get('fx_policy', '远期汇率'), fx_quote_date=market.get('fx_quote_date', ''),
                 sr_price_kind='收盘价' if domestic.get('time_kind') == 'daily_close' else '',
                 sr_quote_date=(domestic.get('quote_date') or domestic.get('quoted_at') or '')[:10])
        quote, mode = r['quote'], payload['quote_mode']
        cfr = None
        if quote is not None:
            if mode == QUOTE_MODES[2]:
                cfr = quote
            elif ice is not None:
                cfr = (ice + quote) * (1 if research else params['polarization']) * params['conversion']
                if mode == QUOTE_MODES[0]:
                    cfr = cfr + r['freight'] if r['freight'] is not None else None
        if cfr is not None and cfr <= 0: cfr = None
        missing = []
        if quote is None: missing.append('CNF报价')
        if mode != QUOTE_MODES[2] and ice is None: missing.append('原糖行情')
        if mode == QUOTE_MODES[0] and r['freight'] is None: missing.append('运费')
        if quote is not None and cfr is None and ice is not None and r['freight'] is not None: missing.append('有效到岸价格')
        if fx is None: missing.append('汇率')
        if sr is None: missing.append('郑糖行情')
        if research:
            fob = (ice + quote) * params['conversion'] if ice is not None and quote is not None else None
            r['insurance'] = fob * params['insurance_rate'] if fob is not None and fob > 0 else None
            if fob is not None and fob <= 0: cfr = None
        else:
            fob = None
        if r['insurance'] is None: missing.append('保险')
        r.update(ice=ice, sr=sr, fx=fx, fob_usd=fob, cfr_usd=cfr, status='缺' + '、'.join(missing) if missing else '输入完整')
        for quota in ('in', 'out'):
            factor = (1 + params['tariff_' + quota]) * (1 + params['vat'])
            def cost(usd):
                if usd is None or fx is None: return None
                taxed = usd * fx * factor
                adjusted = (taxed / (1 - params['loss']) if params['loss_mode'] == '实际损耗'
                            else taxed * (1 + params['loss']))
                if research:
                    adjusted *= (1 + params['bank_rate']) * (1 + params['agency_rate']) * params['polarization']
                    adjusted += params['interest']
                return adjusted + params['processing'] + params['other']
            estimate = cost(cfr)
            actual = cost(cfr + r['insurance']) if cfr is not None and r['insurance'] is not None else None
            r['cost_' + quota] = actual
            r['margin_' + quota] = sr - actual if sr is not None and actual is not None else None
            r['excluding_insurance_' + quota] = sr - estimate if sr is not None and estimate is not None else None
            spot = r['rizhao']
            r['spot_' + quota] = spot - actual if spot is not None and actual is not None else None
        rows.append(r)
    return sorted(rows, key=lambda r: r['period'])


def validate_monthly(payload):
    import re
    from copy import deepcopy
    if not isinstance(payload, dict) or payload.get('schema_version') not in (MONTHLY_VERSION, RESEARCH_VERSION):
        raise ValueError('月份记录版本无效')
    value = deepcopy(payload)
    research = value['schema_version'] == RESEARCH_VERSION
    day = date.fromisoformat(value['date'])
    if day.isoformat() > today(): raise ValueError('不能保存未来业务日')
    if value.get('country') not in COUNTRIES or value.get('quote_mode') not in QUOTE_MODES:
        raise ValueError('国家或报价口径无效，请先选择CNF口径')
    if research and value['quote_mode'] != QUOTE_MODES[0]:
        raise ValueError('新公式使用FOB升贴水与单列海运费')
    if not isinstance(value.get('source'), str) or not value['source'].strip() or len(value['source']) > 4000:
        raise ValueError('请填写CNF及人工参数来源')
    params = value.get('parameters', {})
    if set(params) != set(RESEARCH_PARAMETERS if research else MONTHLY_PARAMETERS): raise ValueError('计算参数集合无效')
    for key, v in params.items():
        if key == 'loss_mode':
            if v not in ('成本加成', '实际损耗'): raise ValueError('损耗口径无效')
        elif isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0:
            raise ValueError('计算参数必须为非负有限数值')
    if any(not 0 <= params[k] < 1 for k in ('tariff_in', 'tariff_out', 'vat', 'loss')):
        raise ValueError('税率及损耗应小于100%')
    if params['polarization'] <= 0 or params['conversion'] <= 0: raise ValueError('换算系数必须为正')
    if research and any(params[k] >= 1 for k in ('insurance_rate', 'bank_rate', 'agency_rate')):
        raise ValueError('费率应小于100%')
    quotes = value.get('quotes')
    if not isinstance(quotes, list) or len(quotes) != 12 or {r['period'] for r in quotes} != set(month_periods(day)):
        raise ValueError('必须保存完整12个月船期')
    for r in quotes:
        sr_pattern = r'SR\d{2}(?:01|05|09)'
        if not re.fullmatch(r'SB[HKNV]\d{2}', r.get('ice_contract', '')) or not re.fullmatch(sr_pattern, r.get('sr_contract', '')):
            raise ValueError('须使用完整ICE和郑糖具体合约')
        ice_month = {v: k for k, v in MONTH_CODES.items()}[r['ice_contract'][2]]
        if (2000 + int(r['ice_contract'][-2:]), ice_month) <= (day.year, day.month):
            raise ValueError('ICE合约已进入交割月或已到期，请核对年份')
        if (2000 + int(r['sr_contract'][2:4]), int(r['sr_contract'][-2:])) <= (day.year, day.month):
            raise ValueError('郑糖合约已进入交割月或已到期，请核对年份')
        for k in ('quote', 'freight', 'insurance', 'rizhao'):
            v = r.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
                                  or (k != 'quote' and v < 0) or (k == 'quote' and value['quote_mode'] == QUOTE_MODES[2] and v <= 0)):
                raise ValueError('报价须为有限数值，费用不能为负；空值请留空')
        if 'freight_origin' in r:
            from .freight import validate_provenance
            validate_provenance(r['freight_origin'], r.get('freight'), day)
        if r.get('rizhao') is not None:
            if not isinstance(r.get('rizhao_date'), str) or date.fromisoformat(r['rizhao_date']) > day:
                raise ValueError('日照现货需填写实际报价日期')
    market = value.get('market')
    if value.get('historical_market') is not None:
        if market is not None: raise ValueError('历史补录与实时快照不能混用')
        from .runtime import historical_market
        historical_market(value['historical_market'], day)
    if market is not None:
        from .runtime import validate_monthly_market
        validate_monthly_market(market, day)
    return value

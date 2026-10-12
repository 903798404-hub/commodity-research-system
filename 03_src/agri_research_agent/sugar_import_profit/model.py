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


"""Shared normalization, validation and atomic persistence for basis entries."""
from __future__ import annotations
import datetime as dt
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
import pandas as pd
import yaml
from io import BytesIO

FIELDS = ["quote_date","variety","variety_code","region","quote_name","futures_contract","contract_year","contract_month","basis","cash_price","unit","source","remarks","raw_text","parse_status","date_inferred","contract_inferred","inference_note","import_method","import_batch_id","source_file","source_sheet","source_row","created_at","updated_at","confirmed","error_message","date","commodity","quote_type","delivery_month","futures_price"]
KEY = ["quote_date","variety_code","region","quote_name","futures_contract","source"]

def config(path: Path) -> dict[str, Any]: return yaml.safe_load(path.read_text(encoding="utf-8"))
def _matches(text: str, cfg: dict[str, Any]) -> list[str]:
    upper = text.upper(); found=[]
    for code, item in cfg["varieties"].items():
        for alias in item["aliases"]:
            token=str(alias).upper()
            matched = bool(re.search(rf"(?<![A-Z]){re.escape(token)}(?![A-Z])", upper)) if len(token) <= 2 and token.isascii() else token in upper
            if matched: found.append(code); break
    return found
def _contract(code: str, text: str, date: dt.date) -> tuple[str | None,bool,str]:
    full = re.findall(rf"{re.escape(code)}\s*(\d{{4}}|\d{{2}})(?:月)?", text.upper())
    found = full or re.findall(r"(?<!\d)(09|01|05)(?:月)?(?!\d)", text)
    if not found: return None, False, ""
    value = found[-1]
    if len(value) == 4: return f"{code}{value}", False, ""
    month = int(value)
    if month not in {1,5,9}: return None, False, "非法合约月份"
    year = date.year + (1 if month < date.month else 0)
    return f"{code}{year % 100:02d}{month:02d}", True, "由月份补全年份"
def _basis(text: str) -> tuple[float | None,str | None]:
    text = re.sub(r"20\d{2}[-/.]?\d{2}[-/.]?\d{2}", "", text)
    vals = re.findall(r"(?:基差\s*)?([+-]\s*\d+(?:\.\d+)?)|(?:升水|贴水)\s*(\d+(?:\.\d+)?)", text)
    parsed=[]
    for signed, plain in vals:
        parsed.append(float(signed.replace(" ","")) if signed else (-float(plain) if "贴水" in text else float(plain)))
    return (parsed[0], None) if len(set(parsed)) == 1 and parsed else (None, "无法识别基差" if not parsed else "多个冲突基差")
def parse_text(text: str, default_date: dt.date, source: str, cfg: dict[str, Any]) -> pd.DataFrame:
    rows=[]
    for raw in [line.strip() for line in text.replace("，", " ").replace(",", " ").replace("\t"," ").splitlines() if line.strip()]:
        now=dt.datetime.now().isoformat(timespec="seconds"); codes=_matches(raw,cfg); status="parsed"; errors=[]
        date_match=re.search(r"(20\d{2})[-/.]?(\d{2})[-/.]?(\d{2})",raw); inferred=not bool(date_match)
        try: qdate=dt.date(*map(int,date_match.groups())) if date_match else default_date
        except ValueError: qdate=default_date; inferred=True; errors.append("日期格式非法")
        if len(codes)!=1: errors.append("无法识别品种" if not codes else "多个冲突品种"); code=None
        else: code=codes[0]
        region=next((r for r in cfg.get("regions",[]) if r in raw), "")
        if not region: errors.append("缺少地区")
        basis,error=_basis(raw)
        if error: errors.append(error)
        contract,contract_inferred,note=_contract(code,raw,qdate) if code else (None,False,"")
        if not contract: errors.append("缺少或非法合约")
        if errors: status="invalid"
        elif inferred or contract_inferred: status="needs_review"
        cashes=[float(x) for x in re.findall(r"(?<![+\-])\b([4-9]\d{3}(?:\.\d+)?)\b",raw)]
        aliases = cfg["varieties"].get(code, {}).get("aliases", []) if code else []
        quote_name = next((str(a) for a in aliases if len(str(a)) > 2 and str(a) in raw), "")
        digits = re.search(r"(\d{4})$", contract or "")
        rows.append({"quote_date":qdate,"variety":cfg["varieties"].get(code,{}).get("name",""),"variety_code":code or "","region":region,"quote_name":quote_name,"futures_contract":contract or "","contract_year":2000 + int(digits.group(1)[:2]) if digits else pd.NA,"contract_month":int(digits.group(1)[-2:]) if digits else pd.NA,"basis":basis,"cash_price":cashes[0] if cashes else pd.NA,"unit":"元/吨","source":source,"remarks":"","raw_text":raw,"parse_status":status,"date_inferred":inferred,"contract_inferred":contract_inferred,"inference_note":note,"import_method":"text","import_batch_id":"","source_file":"","source_sheet":"","source_row":pd.NA,"created_at":now,"updated_at":now,"confirmed":False,"error_message":"；".join(errors)})
    return standardize(pd.DataFrame(rows), cfg)
def parse_fixed_rows(frame: pd.DataFrame, quote_date: dt.date, source: str, cfg: dict[str, Any], method: str) -> pd.DataFrame:
    """Fixed-column table/paste adapter: never infers a contract or a date."""
    mapping={"地区":"region","品种":"variety","基准合约":"futures_contract","基差":"basis","一口价":"cash_price","报价名称":"quote_name","备注":"remarks"}
    data=frame.rename(columns=mapping).copy(); now=dt.datetime.now().isoformat(timespec="seconds")
    for col in mapping.values():
        if col not in data: data[col]=pd.NA
    aliases={str(alias).upper():code for code,item in cfg["varieties"].items() for alias in item["aliases"]}
    data["variety_code"]=data["variety"].astype(str).str.upper().map(aliases)
    data["quote_date"]=quote_date; data["source"]=source; data["unit"]="元/吨"; data["raw_text"]=data.apply(lambda row: "|".join("" if pd.isna(value) else str(value) for value in row),axis=1)
    data["parse_status"]="parsed"; data["date_inferred"]=False; data["contract_inferred"]=False; data["inference_note"]=""; data["import_method"]=method; data["import_batch_id"]=""; data["source_file"]=""; data["source_sheet"]=""; data["source_row"]=pd.NA; data["created_at"]=now; data["updated_at"]=now; data["confirmed"]=False; data["error_message"]=""
    pattern=r"^(Y|OI|P|M|RM)\d{4}$"; bad_contract=~data.futures_contract.astype(str).str.upper().str.match(pattern,na=False)
    missing=data[["region","variety_code","futures_contract"]].isna().any(axis=1) | data.region.astype(str).eq("") | data.quote_name.astype(str).eq("")
    bad_basis=pd.to_numeric(data.basis,errors="coerce").isna()
    invalid=bad_contract|missing|bad_basis
    data.loc[invalid,"parse_status"]="invalid"; data.loc[bad_contract,"error_message"]="基准合约必须为完整代码"; data.loc[missing,"error_message"]="缺少地区、品种或报价名称"; data.loc[bad_basis,"error_message"]="基差必须为数字"
    return standardize(data,cfg,method)
def parse_paste(text: str, quote_date: dt.date, source: str, cfg: dict[str, Any]) -> pd.DataFrame:
    rows=[]
    for line in text.splitlines():
        if not line.strip(): continue
        fields=re.split(r"\||\t|,",line)
        rows.append(fields if len(fields)==7 else fields+[pd.NA]*(7-len(fields)))
    frame=pd.DataFrame(rows,columns=["地区","品种","基准合约","基差","一口价","报价名称","备注"])
    if not frame.empty:
        short=frame.apply(lambda r: r.isna().sum()>0,axis=1); frame.loc[short,"基差"]=pd.NA
    return parse_fixed_rows(frame,quote_date,source,cfg,"paste")

def adapt_excel_result(frame: pd.DataFrame, source: str, cfg: dict[str, Any]) -> pd.DataFrame:
    """Convert the legacy workbook extractor output into the shared fixed-entry schema."""
    rows=[]
    for _, item in frame.iterrows():
        row={"地区":item.get("region",pd.NA),"品种":item.get("commodity",pd.NA),"基准合约":item.get("futures_contract",pd.NA),"基差":item.get("basis",pd.NA),"一口价":item.get("cash_price",pd.NA),"报价名称":item.get("commodity",pd.NA),"备注":item.get("source_sheet","")}
        result=parse_fixed_rows(pd.DataFrame([row]), pd.to_datetime(item.get("date"),errors="coerce").date(), source, cfg, "excel")
        result["raw_text"] = "|".join(str(value) for value in [item.get("date",""), *row.values()])
        result["source_file"] = source; result["source_sheet"] = item.get("source_sheet",pd.NA)
        rows.append(result)
    return pd.concat(rows,ignore_index=True) if rows else pd.DataFrame(columns=FIELDS)
def standardize(frame: pd.DataFrame, cfg: dict[str, Any], method: str | None=None) -> pd.DataFrame:
    data=frame.copy()
    for c in FIELDS:
        if c not in data: data[c]=pd.NA
    data["quote_date"]=pd.to_datetime(data["quote_date"],errors="coerce").dt.date
    data["variety_code"]=data["variety_code"].astype(str).str.upper()
    valid=set(cfg["varieties"]); bad=~data.variety_code.isin(valid)
    data.loc[bad,"parse_status"]="invalid"; data.loc[bad,"error_message"]="不允许的品种，仅允许 Y/OI/P/M/RM"
    data.loc[data.variety_code.isin(valid),"variety"] = data.loc[data.variety_code.isin(valid),"variety_code"].map(lambda x:cfg["varieties"][x]["name"])
    data["basis"]=pd.to_numeric(data.basis,errors="coerce"); data["cash_price"]=pd.to_numeric(data.cash_price,errors="coerce")
    if method: data["import_method"]=method
    data["date"] = data["quote_date"]; data["commodity"] = data["variety"]
    data["quote_type"] = "基差报价"; data["delivery_month"] = data["contract_month"].map(lambda x: f"{int(x):02d}月" if pd.notna(x) else pd.NA)
    return data[FIELDS]
def classify_duplicates(preview: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    data=preview.copy(); data["duplicate_status"]="new"
    if existing.empty:return data
    ex=existing.copy(); ex["quote_date"]=pd.to_datetime(ex.quote_date,errors="coerce").dt.date
    for i,row in data.iterrows():
        hit=ex
        for k in KEY: hit=hit[hit[k].astype(str)==str(row[k])]
        if not hit.empty: data.loc[i,"duplicate_status"]="duplicate_same" if all(str(hit.iloc[-1].get(k,""))==str(row.get(k,"")) for k in ["basis","cash_price","remarks"]) else "duplicate_conflict"
    return data
def write_confirmed(preview: pd.DataFrame, path: Path, backup_dir: Path, overwrite: bool=False) -> dict[str,int]:
    existing=pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=FIELDS); batch=uuid.uuid4().hex; now=dt.datetime.now().isoformat(timespec="seconds")
    # Existing formal data predates the entry schema; enrich it without deleting history.
    for col in FIELDS:
        if col not in existing: existing[col] = pd.NA
    if existing["quote_date"].isna().all() and "date" in existing:
        existing["quote_date"] = pd.to_datetime(existing["date"], errors="coerce").dt.date
        existing["quote_name"] = existing.get("commodity", pd.Series(pd.NA, index=existing.index))
        existing["source"] = "legacy"
    accepted=preview[(preview.parse_status.eq("parsed")) | (preview.parse_status.eq("needs_review") & preview.confirmed.fillna(False))].copy(); accepted=accepted[~accepted.parse_status.eq("invalid")]
    accepted["import_batch_id"]=batch; accepted["updated_at"]=now; success=skipped=overwritten=0
    for _,row in accepted.iterrows():
        mask=pd.Series(True,index=existing.index)
        for k in KEY: mask &= existing[k].astype(str).eq(str(row[k]))
        if mask.any() and not overwrite: skipped+=1; continue
        if mask.any(): existing=existing.loc[~mask]; overwritten+=1
        existing=pd.concat([existing,pd.DataFrame([row])],ignore_index=True); success+=1
    path.parent.mkdir(parents=True,exist_ok=True); backup_dir.mkdir(parents=True,exist_ok=True)
    if path.exists(): shutil.copy2(path,backup_dir/f"basis_quotes_{dt.datetime.now():%Y%m%d_%H%M%S}.parquet")
    tmp=path.with_suffix(".tmp.parquet"); existing.to_parquet(tmp,index=False); os.replace(tmp,path)
    return {"success":success,"skipped":skipped,"overwritten":overwritten,"failed":int((preview.parse_status=="invalid").sum()),"batch_id":batch}

"""Isolated FGIS collector and offline replay using the existing Yearly pipeline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from .fgis import FgisYearlyRangeAdapter as FgisYearlyAdapter
from .fgis import run_fgis_pipeline, _parse_yearly_source, _business_equal, validate_fgis_stable

DOMAIN = 'soybean_export_inspections'
STABLE = f'01_data/processed/{DOMAIN}/soybean_export_inspections_weekly.parquet'
MANIFEST = STABLE.replace('.parquet','.manifest.json')
STATUS = f'01_data/update_status/{DOMAIN}.json'
REVISION = f'01_data/processed/{DOMAIN}/revisions/soybean_export_inspections_revisions.parquet'
BASELINE_FILES = (STABLE, MANIFEST, STATUS, REVISION)


def identity(path):
    path=Path(path)
    if not path.exists() and not path.is_symlink():return None
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1:
        raise ValueError('FGIS input must be a regular independent file')
    raw=Path(path).read_bytes()
    return {'sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw)}


def seed(runtime, baseline, request):
    for relative in BASELINE_FILES:
        source=baseline/relative
        if identity(source)!=request['baseline'][relative]:
            raise ValueError('FGIS production baseline changed')
        if request['baseline'][relative] is None:
            if relative!=REVISION:raise ValueError('FGIS required baseline is absent')
            continue
        target=runtime/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)


def revision_history(runtime, baseline, *, replay=None):
    """Keep every old audit row and independently replay the appended values."""
    import pandas as pd
    prior=baseline/REVISION;current=runtime/REVISION
    old=pd.read_parquet(prior) if prior.exists() else None
    new=pd.read_parquet(current) if current.exists() else None
    old_count=0 if old is None else len(old)
    if old is not None:
        if new is None or len(new)<old_count:raise ValueError('FGIS revision history was removed')
        pd.testing.assert_frame_equal(old.reset_index(drop=True),new.iloc[:old_count].reset_index(drop=True),check_dtype=False,check_exact=True)
    if replay is not None:
        expected_path=replay/'project'/REVISION
        expected=pd.read_parquet(expected_path) if expected_path.exists() else None
        if (expected is None)!=(new is None):raise ValueError('FGIS replay revision log differs')
        if expected is not None:
            if old is not None:
                pd.testing.assert_frame_equal(old.reset_index(drop=True),expected.iloc[:old_count].reset_index(drop=True),check_dtype=False,check_exact=True)
            # Only the newly detected timestamp differs between executions.
            columns=[c for c in expected.columns if c!='detected_at_utc']
            pd.testing.assert_frame_equal(expected.iloc[old_count:][columns].reset_index(drop=True),new.iloc[old_count:][columns].reset_index(drop=True),check_dtype=False,check_exact=True)
    return {'baseline_identity':identity(prior),'baseline_rows':old_count,
            'candidate_rows':0 if new is None else len(new),'appended_rows':0 if new is None else len(new)-old_count}


def prepare(baseline: Path, output: Path, request: dict, *, replay: Path | None=None):
    """No production writes. All native pipeline outputs stay under output."""
    import pandas as pd
    runtime=output/'project';runtime.mkdir(parents=True,exist_ok=False)
    seed(runtime,baseline,request)
    if replay is None:
        adapter=FgisYearlyAdapter(timeout_seconds=60,use_environment_proxy=False)
    else:
        info=json.loads((replay/'collection.json').read_bytes())
        source=replay/info['source_relative_path'];raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest().upper()!=info['source_sha256'].upper():
            raise ValueError('FGIS sealed CSV identity mismatch')
        class ArchivedAdapter:
            def fetch_soybeans(self,**kwargs):
                return _parse_yearly_source(content=raw,calendar_year=info['calendar_year'],
                    source_file=info['source_file'],source_url=info['source_url'],
                    fetched_at=info['fetch_time_utc'],http_status=200,content_length=len(raw),
                    etag=info['etag'],last_modified=info['last_modified'],delivery='https')
        adapter=ArchivedAdapter()
    audit=run_fgis_pipeline(runtime_root=runtime,adapter=adapter,
        git_head=request['commit'],batch_id=request['run_id'],publish=True)
    history=revision_history(runtime,baseline,replay=replay)
    if audit['status']=='no_change':
        result={'status':'NO_CHANGE','audit':audit,'files':{p:identity(runtime/p) for p in BASELINE_FILES}}
    else:
        manifest=json.loads((runtime/MANIFEST).read_bytes())
        raw_dir=Path(audit['raw_snapshot_dir']);raw_manifest=json.loads((raw_dir/'manifest.json').read_bytes())
        source=raw_dir/manifest['source_file'];df=pd.read_parquet(runtime/STABLE)
        result={'status':'CANDIDATE','audit':audit,'files':{p:identity(runtime/p) for p in BASELINE_FILES},
            'source_relative_path':str(source.relative_to(output)),
            **{k:manifest[k] for k in ['source_sha256','calendar_year','source_file','source_url','etag','last_modified']},
            'fetch_time_utc':raw_manifest['fetch_time_utc'],'validation':validate_fgis_stable(df)}
        if replay is not None:
            expected=pd.read_parquet(replay/'project'/STABLE)
            if not _business_equal(expected,df):
                raise ValueError('FGIS offline replay differs from collected business data')
            result['offline_replay']='PASS'
    result['revision_history']=history
    (output/'collection.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--replay',action='store_true');args=parser.parse_args()
    request=json.loads(Path('/baseline/request.json').read_bytes())
    release=json.loads(Path('/app/RELEASE.json').read_bytes())
    if release['git_commit']!=request['commit'] or release['git_tree']!=request['tree']:
        raise ValueError('FGIS worker image identity differs')
    result=prepare(Path('/runtime'),Path('/output'),request,replay=Path('/candidate') if args.replay else None)
    print(json.dumps({'status':result['status'],'latest':result['audit'].get('latest'),'validation':result.get('validation'),'offline_replay':result.get('offline_replay')}))
    return 0

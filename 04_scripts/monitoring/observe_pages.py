"""Network-free read-only worker. All paths are explicit, host-approved mounts."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import subprocess
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "03_src"))
from agri_research_agent.monitoring.catalog import RULES
from agri_research_agent.monitoring.freshness import Observation, evaluate


def group(identity):
    if identity.startswith(('positions_','foreign_positions_')):
        return 'positions'
    for prefix in ['fx_','nass_','canada_','brazil_','warehouse_','weather_']:
        if identity.startswith(prefix):
            return prefix
    if identity in {'public_market','public_fx'}:
        return 'public_market'
    return identity


def main():
    if len(sys.argv) == 1:
        # Native validators may allocate temporary histories. Release the whole
        # child after each domain, instead of accumulating unrelated dataframes
        # in a long-lived monitor or the website's production process.
        rows = []
        blocked=json.loads(os.environ.get('PAGE_MONITOR_BLOCKED_INPUTS','{}'))
        if not isinstance(blocked,dict) or not set(blocked)<={'usda','oil_world','guangxi','yunnan'} or any(not isinstance(v,str) for v in blocked.values()):
            raise ValueError('Unknown blocked reader inputs')
        groups = dict.fromkeys(group(rule.identity) for rule in RULES)
        for key in groups:
            rules = [r for r in RULES if group(r.identity)==key]
            if key in blocked:
                rows.extend(evaluate(r,Observation(None,error=blocked[key],retry_safe=False),datetime.now(timezone.utc)) for r in rules)
                continue
            try:
                result = subprocess.run([sys.executable,'-B',__file__,'--group',key],
                    capture_output=True,timeout=120,check=True)
                rows.extend(json.loads(result.stdout))
                sys.stderr.buffer.write(result.stderr)
            except (subprocess.SubprocessError,ValueError) as exc:
                sys.stderr.buffer.write((getattr(exc,'stderr',None) or b'')[-64*1024:])
                rows.extend(evaluate(r,Observation(None,error='该来源只读核验失败或超过资源/时间上限，等待核查',retry_safe=False),datetime.now(timezone.utc)) for r in rules)
        print(json.dumps(rows,ensure_ascii=False,allow_nan=False))
        return
    if len(sys.argv)!=3 or sys.argv[1]!='--group' or sys.argv[2] not in {group(r.identity) for r in RULES}:
        raise ValueError('Unknown monitoring group')
    from agri_research_agent.monitoring.observations import Inputs, collect
    inputs = Inputs(data=Path('/inputs/data'), manual=Path('/inputs/manual'),
        usda=Path('/inputs/usda'), oil_world=Path('/inputs/oil_world'),
        guangxi=Path('/inputs/guangxi'), yunnan=Path('/inputs/yunnan'), source=Path('/app'))
    identities={r.identity for r in RULES if group(r.identity)==sys.argv[2]}
    print(json.dumps(collect(inputs, datetime.now(timezone.utc),identities), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()

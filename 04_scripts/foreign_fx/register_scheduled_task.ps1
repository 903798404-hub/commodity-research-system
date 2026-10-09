param([Parameter(Mandatory=$true)][string]$ConfigPath, [switch]$Install)
$ErrorActionPreference = 'Stop'
$taskConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$taskConfig = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskControlRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'FX task requires the verified China Standard Time Windows host' }
$taskUserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrepareCode = @'
from pathlib import Path
import sys, importlib.util
from datetime import datetime, timezone, timedelta
root, config_path, user_id, install = sys.argv[1:]
spec=importlib.util.spec_from_file_location('fx_task_entry',Path(root)/'04_scripts/automation/run_production_data_delta_windows.py')
entry=importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
def closed(pairs):
    out={}
    for key,value in pairs:
        if key in out: raise ValueError('duplicate FX configuration key')
        out[key]=value
    return out
config=entry.json.loads(Path(config_path).read_text(encoding='utf-8'),object_pairs_hook=closed)
if install == 'true': entry._bootstrap(config)
module=entry.load_module()
if install == 'true': module.verify_clean_detached_clone(Path(root),config)
sys.path.insert(0,str(Path(root)/'03_src'))
from agri_research_agent.automation.production_data_delta_fx import validate_config,task_xml
validate_config(config)
runtime=Path(config['runtime_root']).resolve()
for other in (Path(root).resolve(),Path(config['baseline_root']).resolve()):
    if runtime.is_relative_to(other) or other.is_relative_to(runtime): raise ValueError('FX task runtime overlaps source or baseline')
runtime.mkdir(parents=True,exist_ok=True)
output=module._unlinked(runtime/'foreign-fx-task.xml')
output.write_bytes(task_xml(control_root=root,config_path=config_path,user_id=user_id,start_date=datetime.now(timezone(timedelta(hours=8))).date()))
print(str(output))
'@
$taskXmlPath = & $taskConfig.python -I -B -X utf8 -c $taskPrepareCode $taskControlRoot $taskConfigPath $taskUserId ($Install.IsPresent.ToString().ToLowerInvariant())
if ($LASTEXITCODE -ne 0) { throw 'FX task preparation failed; nothing registered' }
if (-not $Install) { Write-Output $taskXmlPath; exit 0 }
$taskName = 'MarketData-Xiaoran-foreign-fx'
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting) {
    $taskBackup = Join-Path $taskConfig.runtime_root ('foreign-fx-task-before-' + (Get-Date -Format 'yyyyMMddTHHmmssfff') + '.xml')
    [IO.File]::WriteAllText($taskBackup, (Export-ScheduledTask -TaskName $taskName), [Text.UTF8Encoding]::new($false))
}
Register-ScheduledTask -TaskName $taskName -Xml ([IO.File]::ReadAllText($taskXmlPath)) -Force | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime,LastTaskResult,NextRunTime

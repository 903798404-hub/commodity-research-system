param([Parameter(Mandatory=$true)][string]$ConfigPath, [switch]$Install)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Scheduled jobs require the verified China Standard Time host' }
$taskConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$taskConfig = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskControlRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskUserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrepareCode = @'
import importlib.util, json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
root, config_path, user_id = sys.argv[1:]
spec = importlib.util.spec_from_file_location('scheduled_entry', Path(root)/'04_scripts/automation/run_first_batch_scheduled.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
def closed(pairs):
    out={}
    for key,value in pairs:
        if key in out: raise ValueError('duplicate scheduled configuration key')
        out[key]=value
    return out
config=json.loads(Path(config_path).read_text(encoding='utf-8'), object_pairs_hook=closed)
entry.bootstrap(config)
from agri_research_agent.automation.first_batch_scheduled import validate_config, task_xml
from agri_research_agent.data_sources.nutstore_basis import assert_external_output
validate_config(config)
assert_external_output(config_path)
output=assert_external_output(Path(config['runtime_root'])/'task-preview'/('first-batch-'+config['job']+'.xml'))
output.parent.mkdir(parents=True,exist_ok=True)
output.write_bytes(task_xml(config=config,control_root=root,config_path=config_path,user_id=user_id,start_date=datetime.now(timezone(timedelta(hours=8))).date()))
print(str(output))
'@
$taskXmlPath = & $taskConfig.delivery.python -I -B -X utf8 -c $taskPrepareCode $taskControlRoot $taskConfigPath $taskUserId
if ($LASTEXITCODE -ne 0) { throw 'Scheduled task preparation failed; nothing registered' }
if (-not $Install) { Write-Output $taskXmlPath; exit 0 }
$taskName = 'MarketData-Xiaoran-' + $taskConfig.job.Replace('_','-')
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting) {
    $taskBackup = Join-Path ([IO.Path]::GetDirectoryName($taskXmlPath)) ($taskName + '-before-' + (Get-Date -Format 'yyyyMMddTHHmmssfff') + '.xml')
    [IO.File]::WriteAllText($taskBackup, (Export-ScheduledTask -TaskName $taskName), [Text.UTF8Encoding]::new($false))
}
Register-ScheduledTask -TaskName $taskName -Xml ([IO.File]::ReadAllText($taskXmlPath)) -Force | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime,LastTaskResult,NextRunTime

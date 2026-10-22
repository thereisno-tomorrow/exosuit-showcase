param(
    [string]$TaskName = 'GremlinTown-PC-Backup',
    [string]$ScriptPath = (Join-Path $PSScriptRoot 'backup-from-pc.ps1')
)
$ErrorActionPreference = 'Stop'
if ($env:GT_APPROVED_PC_BACKUP -ne 'YES') { throw 'Set GT_APPROVED_PC_BACKUP=YES after approval.' }
if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) { throw "Backup script missing: $ScriptPath" }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw "Scheduled task already exists: $TaskName" }

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$escapedScript = $ScriptPath.Replace("'", "''")
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -Command `"& { `$env:GT_APPROVED_PC_BACKUP='YES'; & '$escapedScript' }`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At '04:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Encrypted Gremlin Town backup to the existing PC; no forget or prune.' | Out-Null
Write-Host "Registered $TaskName for $user at 04:00 local time; no password was stored in task arguments."

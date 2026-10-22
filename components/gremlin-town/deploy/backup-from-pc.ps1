param(
    [string]$Restic = 'C:\Example\Tools\restic\restic.exe',
    [string]$Repository = 'C:\Example\Backups\gremlin-town',
    [string]$PasswordFile = 'C:\Example\.secrets\gremlin-town\restic-password.txt',
    [string]$Remote = 'operator@hub.example.invalid'
)
$ErrorActionPreference = 'Stop'
if ($env:GT_APPROVED_PC_BACKUP -ne 'YES') { throw 'Set GT_APPROVED_PC_BACKUP=YES after approval.' }
foreach ($path in @($Restic, $PasswordFile)) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" } }
if (-not [IO.Path]::IsPathRooted($Repository)) { throw 'Backup repository must be an absolute path.' }
$repositoryPath = [IO.Path]::GetFullPath($Repository)
$repositoryRoot = [IO.Path]::GetPathRoot($repositoryPath)
if ([string]::IsNullOrWhiteSpace($repositoryRoot)) { throw 'Backup repository has no destination root.' }
$destinationDrive = [IO.DriveInfo]::new($repositoryRoot)
if (-not $destinationDrive.IsReady) { throw "Backup destination is not ready: $repositoryRoot" }
$availableFreeSpace = $destinationDrive.AvailableFreeSpace
if ($availableFreeSpace -lt 10GB) { throw 'Backup refused: less than 10 GiB free on destination drive.' }
if ($availableFreeSpace -lt 20GB) { Write-Warning 'Less than 20 GiB free on destination drive.' }
$env:RESTIC_REPOSITORY = $Repository
$env:RESTIC_PASSWORD_FILE = $PasswordFile
if (-not (Test-Path -LiteralPath (Join-Path $Repository 'config'))) {
    & $Restic init
    if ($LASTEXITCODE -ne 0) { throw "restic init failed: $LASTEXITCODE" }
}
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
& $Restic backup --host 'example-hub' --tag 'gremlin-town' `
    --stdin-filename "gremlin-town-$stamp.tar" --stdin-from-command -- `
    ssh $Remote sudo -u gremlin-town /usr/local/lib/gremlin-town/snapshot-stream.sh
if ($LASTEXITCODE -ne 0) { throw "restic backup failed: $LASTEXITCODE" }
& $Restic check
if ($LASTEXITCODE -ne 0) { throw "restic check failed: $LASTEXITCODE" }
Write-Host 'Encrypted PC backup completed. No snapshot was forgotten or pruned.'

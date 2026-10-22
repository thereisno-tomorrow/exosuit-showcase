param(
    [Parameter(Mandatory=$true)][string]$SnapshotId,
    [string]$Restic = 'C:\Example\Tools\restic\restic.exe',
    [string]$Repository = 'C:\Example\Backups\gremlin-town',
    [string]$PasswordFile = 'C:\Example\.secrets\gremlin-town\restic-password.txt',
    [string]$Remote = 'operator@hub.example.invalid'
)
$ErrorActionPreference = 'Stop'
if ($env:GT_APPROVED_RESTORE -ne 'YES') { throw 'Set GT_APPROVED_RESTORE=YES after restore approval.' }
if ($SnapshotId -notmatch '^[0-9a-f]{8,64}$') { throw 'Use an exact restic snapshot ID.' }
$target = "C:\Example\Backups\gremlin-town-restore\$SnapshotId"
if (Test-Path -LiteralPath $target) { throw "Restore target exists: $target" }
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$null = New-Item -ItemType Directory -Path $target
& icacls.exe $target /inheritance:r /grant:r "${currentIdentity}:(OI)(CI)F"
if ($LASTEXITCODE -ne 0) { throw "failed to restrict restore ACL: $LASTEXITCODE" }
$env:RESTIC_REPOSITORY = $Repository
$env:RESTIC_PASSWORD_FILE = $PasswordFile
& $Restic restore $SnapshotId --target $target
if ($LASTEXITCODE -ne 0) { throw "restic restore failed: $LASTEXITCODE" }
$archives = @(Get-ChildItem -LiteralPath $target -Recurse -Filter 'gremlin-town-*.tar')
if ($archives.Count -ne 1) { throw "Expected exactly one restored tar, found $($archives.Count)." }
$archive = $archives[0]
ssh $Remote "install -d -o root -g root -m 0700 /var/lib/gremlin-town/restore/incoming"
if ($LASTEXITCODE -ne 0) { throw 'remote restore directory preparation failed' }
scp $archive.FullName "${Remote}:/var/lib/gremlin-town/restore/incoming/$SnapshotId.tar"
if ($LASTEXITCODE -ne 0) { throw 'restore archive transfer failed' }
ssh $Remote "GT_APPROVED_RESTORE=YES /usr/local/lib/gremlin-town/restore.sh /var/lib/gremlin-town/restore/incoming/$SnapshotId.tar /var/lib/gremlin-town/restore/$SnapshotId"
if ($LASTEXITCODE -ne 0) { throw 'isolated host restore verification failed' }
Write-Host 'Restore is isolated and verified. It has not replaced the live database.'

param([string]$InstallDir = 'C:\Example\Tools\restic')
$ErrorActionPreference = 'Stop'
if ($env:GT_APPROVED_PC_BACKUP -ne 'YES') { throw 'Set GT_APPROVED_PC_BACKUP=YES after approval.' }
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') { throw 'This package is pinned for Windows x64.' }
$version = '0.19.1'
$archiveName = "restic_${version}_windows_amd64.zip"
$expectedSha256 = 'da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d'
$baseUrl = "https://github.com/restic/restic/releases/download/v$version"
$downloadDir = Join-Path $InstallDir '.download-0.19.1'
if (Test-Path -LiteralPath $InstallDir) { throw "Install path already exists: $InstallDir" }
New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
$archive = Join-Path $downloadDir $archiveName
$sums = Join-Path $downloadDir 'SHA256SUMS'
Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/$archiveName" -OutFile $archive
Invoke-WebRequest -UseBasicParsing -Uri "$baseUrl/SHA256SUMS" -OutFile $sums
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
if ($actual -ne $expectedSha256) { throw "Archive hash mismatch: $actual" }
$officialLine = @(Get-Content -LiteralPath $sums | Where-Object { $_ -match "\s+$([regex]::Escape($archiveName))$" })
if ($officialLine.Count -ne 1) { throw 'Official SHA256SUMS entry missing or duplicated.' }
$official = ($officialLine[0] -split '\s+')[0].ToLowerInvariant()
if ($official -ne $expectedSha256) { throw "Official checksum differs from reviewed pin: $official" }
Expand-Archive -LiteralPath $archive -DestinationPath $downloadDir
$binaries = @(Get-ChildItem -LiteralPath $downloadDir -Filter 'restic*.exe')
if ($binaries.Count -ne 1) { throw "Expected one restic executable, found $($binaries.Count)." }
Move-Item -LiteralPath $binaries[0].FullName -Destination (Join-Path $InstallDir 'restic.exe')
$versionOutput = (& (Join-Path $InstallDir 'restic.exe') version) -join "`n"
if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch '^restic 0\.19\.1\b') { throw "Unexpected restic version: $versionOutput" }
Write-Host $versionOutput
Write-Host "Installed only $InstallDir\restic.exe plus retained verification files in $downloadDir."

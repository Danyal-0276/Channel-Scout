param(
    [Parameter(Mandatory)]
    [ValidateSet('discover','draft','sync-sheets')]
    [string]$Job
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'
New-Item -Path $logDir -ItemType Directory -Force | Out-Null
$log = Join-Path $logDir "$Job.log"
$previous = Join-Path $logDir "$Job.previous.log"
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
    Move-Item -LiteralPath $log -Destination $previous -Force
}
"$(Get-Date -Format o) start $Job" | Out-File -LiteralPath $log -Append -Encoding utf8
& $python -m channel_scout.cli --project-dir $root $Job 2>&1 | Out-File -LiteralPath $log -Append -Encoding utf8
$result = $LASTEXITCODE
"$(Get-Date -Format o) exit $result" | Out-File -LiteralPath $log -Append -Encoding utf8
exit $result

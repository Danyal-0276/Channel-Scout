param(
    [switch]$IncludeDiscoveryAndSync,
    [switch]$IncludeDrafting
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the ChannelScout virtual environment first.' }
if (-not ($IncludeDiscoveryAndSync -or $IncludeDrafting)) {
    throw 'Specify -IncludeDiscoveryAndSync, -IncludeDrafting, or both.'
}

$selected = @()
if ($IncludeDiscoveryAndSync) {
    $selected += @{ Name = 'ChannelScout Discovery'; Command = 'discover'; Offset = 1 }
    $selected += @{ Name = 'ChannelScout Sheet Sync'; Command = 'sync-sheets'; Offset = 3 }
}
if ($IncludeDrafting) {
    $selected += @{ Name = 'ChannelScout Drafting'; Command = 'draft'; Offset = 5 }
}

foreach ($job in $selected) {
    if (Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue) {
        throw "Task already exists: $($job.Name). Remove it or edit it in Task Scheduler before re-registering."
    }
}
foreach ($job in $selected) {
    $action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $root -Argument "-m channel_scout.cli $($job.Command)"
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($job.Offset) -RepetitionInterval (New-TimeSpan -Minutes 15)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 12)
    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger -Settings $settings -Description 'ChannelScout recurring job. Runs when the current Windows user is signed in.' | Out-Null
    Write-Output "Registered $($job.Name)."
}

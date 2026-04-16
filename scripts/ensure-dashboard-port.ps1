param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$DashboardArgs
)

$port = 8765

for ($index = 0; $index -lt $DashboardArgs.Length; $index++) {
  if ($DashboardArgs[$index] -eq "--port" -and $index + 1 -lt $DashboardArgs.Length) {
    $candidate = $DashboardArgs[$index + 1]
    if ($candidate -match '^\d+$') {
      $port = [int]$candidate
    }
  }
}

$listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique

if (-not $listeners) {
  exit 0
}

foreach ($listenerPid in $listeners) {
  if (-not ($listenerPid -as [int])) {
    continue
  }

  $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid" -ErrorAction SilentlyContinue
  if ($null -eq $processInfo) {
    continue
  }

  $commandLine = [string]$processInfo.CommandLine
  if ($commandLine -match 'openclaw_adapter\s+serve-dashboard') {
    Write-Host "[INFO] Stopping stale OpenClaw dashboard on port $port (PID $listenerPid)."
    Stop-Process -Id $listenerPid -Force -ErrorAction Stop
    Start-Sleep -Seconds 1
    continue
  }

  Write-Host "[ERROR] Port $port is already in use by another process."
  Write-Host "[ERROR] PID: $listenerPid"
  Write-Host "[ERROR] CommandLine: $commandLine"
  exit 1
}

exit 0

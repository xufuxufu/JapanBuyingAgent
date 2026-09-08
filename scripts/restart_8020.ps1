#requires -Version 5.1
<#
Restarts the JBA dev server on port 8020, reliably.

Root cause of the old restart_dev_8020_py314.bat: it used
`netstat -ano | findstr ":8020 .*LISTENING"`. Without /C or /R, findstr
splits its argument on spaces and OR-matches each word literally -- so this
matched any line containing the plain substring ":8020", including
tailscaled's own listeners on the Tailscale interface and unrelated
ESTABLISHED connections that merely reference port 8020. taskkill was then
sometimes handed the wrong PID (or none), so the real uvicorn process
frequently survived the "restart".

This script only ever stops a process that is actually LISTENING on
0.0.0.0:8020 or 127.0.0.1:8020 -- i.e. the app's own bind
(`--host 0.0.0.0 --port 8020`) -- never Tailscale's own addresses and never
an incidental ESTABLISHED connection. It verifies the port is actually free
before proceeding, so a stuck process is reported instead of silently
ignored.
#>

$ErrorActionPreference = 'Stop'
$port = 8020
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

function Get-ListeningOwners {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq '0.0.0.0' -or $_.LocalAddress -eq '127.0.0.1' } |
        Select-Object -ExpandProperty OwningProcess -Unique
}

Write-Host "Checking for an existing process on ${port} (0.0.0.0/127.0.0.1 only) ..."
$targets = @(Get-ListeningOwners)

if ($targets.Count -eq 0) {
    Write-Host "Nothing listening on ${port} -- nothing to stop."
} else {
    foreach ($procId in $targets) {
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            Write-Host "Stopping PID $procId ($($proc.ProcessName)) ..."
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-Host "WARN: could not stop PID ${procId}: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 1
    $stillListening = @(Get-ListeningOwners)
    if ($stillListening.Count -gt 0) {
        Write-Host "ERROR: port $port is still bound after stop attempt (PID(s): $($stillListening -join ',')). Aborting restart."
        exit 1
    }
    Write-Host "Old process(es) confirmed stopped."
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "ERROR: venv python not found at $venvPython -- run run_dev_8020_py314.bat once to create it."
    exit 1
}

Write-Host "Running alembic upgrade head ..."
& $venvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: alembic upgrade head failed (exit $LASTEXITCODE). Not starting uvicorn."
    exit 1
}

Write-Host "Starting uvicorn on 0.0.0.0:$port ..."
& $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port $port
exit $LASTEXITCODE

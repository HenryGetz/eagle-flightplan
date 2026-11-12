# UNW planner bootstrap (Windows PowerShell).
# If Windows blocks the script: Set-ExecutionPolicy -Scope Process Bypass
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python {
    $candidates = @(
        @("py", "-3"),
        @("python", "")
    )
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $flag = $candidate[1]
        try {
            if ($flag) {
                & $exe $flag -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return @($exe, $flag) }
            } else {
                & $exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return @($exe, "") }
            }
        } catch {
            continue
        }
    }
    return $null
}

$python = Find-Python
if ($null -eq $python) {
    Write-Host "Python 3.11+ was not found."
    Write-Host "Install Python 3.12 from https://www.python.org/downloads/ and tick 'Add Python to PATH'."
    exit 1
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv ..."
    if ($python[1]) {
        & $python[0] $python[1] -m venv .venv
    } else {
        & $python[0] -m venv .venv
    }
}

Write-Host "Installing the planner package ..."
& $venvPython -m pip install --quiet --upgrade pip | Out-Null
& $venvPython -m pip install --quiet -e .
if ($LASTEXITCODE -ne 0) {
    Write-Host "Package install failed. Run: .venv\Scripts\python.exe -m pip install -e ."
    exit 1
}

& $venvPython -m unw_planner.cli setup @args
exit $LASTEXITCODE

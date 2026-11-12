# One-command bootstrap for Windows: clone/update the repo, then run install.ps1.
# irm https://raw.githubusercontent.com/HenryGetz/FlightPlan/main/bootstrap.ps1 | iex
$ErrorActionPreference = "Stop"

$Repo = if ($env:FLIGHTPLAN_REPO) { $env:FLIGHTPLAN_REPO } else { "https://github.com/HenryGetz/FlightPlan.git" }
$HomeDir = if ($env:FLIGHTPLAN_HOME) { $env:FLIGHTPLAN_HOME } elseif ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$Dest = if ($env:FLIGHTPLAN_HOME) { $env:FLIGHTPLAN_HOME } else { Join-Path $HomeDir ".flightplan" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required. Install Git for Windows, then run this command again."
}

if (Test-Path (Join-Path $Dest ".git")) {
    Write-Host "Updating $Dest ..."
    git -C "$Dest" pull --ff-only
} elseif (Test-Path $Dest) {
    throw "$Dest exists but is not a git checkout; move it aside or set FLIGHTPLAN_HOME."
} else {
    Write-Host "Cloning $Repo to $Dest ..."
    git clone --depth 1 -- "$Repo" "$Dest"
}

& (Join-Path $Dest "install.ps1") @args
if ($LASTEXITCODE -ne 0) {
    throw "install.ps1 failed with exit code $LASTEXITCODE"
}

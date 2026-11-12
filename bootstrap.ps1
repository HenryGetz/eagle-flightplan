# One-command bootstrap for Windows: clone/update the repo, then run install.ps1.
# irm https://raw.githubusercontent.com/HenryGetz/eagle-flightplan/main/bootstrap.ps1 | iex
$ErrorActionPreference = "Stop"

$Repo = if ($env:EAGLE_FLIGHTPLAN_REPO) { $env:EAGLE_FLIGHTPLAN_REPO } else { "https://github.com/HenryGetz/eagle-flightplan.git" }
$HomeDir = if ($env:EAGLE_FLIGHTPLAN_HOME) { $env:EAGLE_FLIGHTPLAN_HOME } elseif ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$Dest = if ($env:EAGLE_FLIGHTPLAN_HOME) { $env:EAGLE_FLIGHTPLAN_HOME } else { Join-Path $HomeDir ".eagle-flightplan" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required. Install Git for Windows, then run this command again."
}

if (Test-Path (Join-Path $Dest ".git")) {
    Write-Host "Updating Eagle Flightplan at $Dest ..."
    git -C "$Dest" pull --ff-only
} elseif (Test-Path $Dest) {
    throw "$Dest exists but is not a git checkout; move it aside or set EAGLE_FLIGHTPLAN_HOME."
} else {
    Write-Host "Cloning Eagle Flightplan to $Dest ..."
    git clone --depth 1 -- "$Repo" "$Dest"
}

& (Join-Path $Dest "install.ps1") @args
if ($LASTEXITCODE -ne 0) {
    throw "install.ps1 failed with exit code $LASTEXITCODE"
}

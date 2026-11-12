# One-command bootstrap for Windows: clone/update the repo, then run install.ps1.
# irm https://raw.githubusercontent.com/HenryGetz/unw-planner/main/bootstrap.ps1 | iex
$ErrorActionPreference = "Stop"

$Repo = if ($env:UNW_PLANNER_REPO) { $env:UNW_PLANNER_REPO } else { "https://github.com/HenryGetz/unw-planner.git" }
$HomeDir = if ($env:UNW_PLANNER_HOME) { $env:UNW_PLANNER_HOME } elseif ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
$Dest = if ($env:UNW_PLANNER_HOME) { $env:UNW_PLANNER_HOME } else { Join-Path $HomeDir ".unw-planner" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required. Install Git for Windows, then run this command again."
}

if (Test-Path (Join-Path $Dest ".git")) {
    Write-Host "Updating $Dest ..."
    git -C "$Dest" pull --ff-only
} elseif (Test-Path $Dest) {
    throw "$Dest exists but is not a git checkout; move it aside or set UNW_PLANNER_HOME."
} else {
    Write-Host "Cloning $Repo to $Dest ..."
    git clone --depth 1 -- "$Repo" "$Dest"
}

& (Join-Path $Dest "install.ps1") @args
if ($LASTEXITCODE -ne 0) {
    throw "install.ps1 failed with exit code $LASTEXITCODE"
}

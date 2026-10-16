# codex-auto-resume uninstaller for native Windows (PowerShell 5.1+).
#
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -Purge
#
# Removes the helper task, the plugin, the marketplace entry and (with -Purge)
# the state directory. Nothing else is touched.
param(
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) { Write-Host "==> $Message" }
function Write-Warn([string]$Message) { Write-Warning $Message }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Launcher = Join-Path $RepoRoot "bin\codex-auto-resume"

$StateDir = $env:CODEX_AUTO_RESUME_STATE_DIR
if (-not $StateDir) {
    $Base = $env:LOCALAPPDATA
    if (-not $Base) { $Base = Join-Path $env:USERPROFILE "AppData\Local" }
    $StateDir = Join-Path $Base "codex-auto-resume"
}

$Python = $null
foreach ($Candidate in @("python.exe", "py.exe", "python3.exe")) {
    $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($Found) { $Python = $Found.Source; break }
}

if ($Python -and (Test-Path $Launcher)) {
    Write-Step "stopping automatic resume"
    & $Python $Launcher disable 2>$null
    Write-Step "removing the helper task"
    & $Python $Launcher uninstall-service 2>$null
} else {
    Write-Warn "Python or the launcher is missing; removing the scheduled task directly"
    schtasks /delete /tn "codex-auto-resume-helper" /f 2>$null
}

$Codex = Get-Command codex -ErrorAction SilentlyContinue
if ($Codex) {
    Write-Step "removing the plugin and marketplace"
    & $Codex.Source plugin remove "codex-auto-resume@codex-auto-resume" 2>$null
    & $Codex.Source plugin marketplace remove "codex-auto-resume" 2>$null
} else {
    Write-Warn "codex CLI not found; remove the plugin and marketplace manually"
}

if ($Purge) {
    Write-Step "deleting state at $StateDir"
    Remove-Item -Recurse -Force $StateDir -ErrorAction SilentlyContinue
} else {
    Write-Step "keeping state at $StateDir (use -Purge to delete it)"
}

Write-Host "Done."

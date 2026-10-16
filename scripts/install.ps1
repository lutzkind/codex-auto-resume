# codex-auto-resume installer for native Windows (PowerShell 5.1+).
#
# Steps, identical to scripts/install.sh:
#   1. locate Python 3.9+ and the codex CLI
#   2. enable the Codex `plugins` feature if needed
#   3. add this checkout as a Codex plugin marketplace and install the plugin
#   4. persist hook trust through Codex's own config API
#   5. install the per-user Task Scheduler task that does the waiting
#
# Everything here is additive and per-user: no administrator rights, no
# machine-wide changes, no modification of existing Codex configuration beyond
# the plugin and its hook trust.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) { Write-Host "==> $Message" }
function Write-Warn([string]$Message) { Write-Warning $Message }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Resolve-Python {
    foreach ($Candidate in @("python.exe", "py.exe", "python3.exe")) {
        $Found = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Found) { return $Found.Source }
    }
    return $null
}

$Python = Resolve-Python
if (-not $Python) {
    throw "Python 3.9+ was not found on PATH. Install Python from https://python.org and reopen PowerShell."
}

$Launcher = Join-Path $RepoRoot "bin\codex-auto-resume"
if (-not (Test-Path $Launcher)) { throw "bin\codex-auto-resume is missing from $RepoRoot" }

$Codex = $env:CODEX_AUTO_RESUME_CODEX_BIN
if (-not $Codex) {
    $Found = Get-Command codex -ErrorAction SilentlyContinue
    if ($Found) { $Codex = $Found.Source }
}
if (-not $Codex) { throw "The 'codex' CLI was not found on PATH." }

Write-Step "Python: $Python"
Write-Step "Codex:  $Codex"
& $Codex --version

$Features = & $Codex features list 2>$null
if ($Features -match "(?m)^\s*plugins\s+(\S+)") {
    if ($Matches[1] -eq "false") {
        Write-Step "enabling the Codex 'plugins' feature"
        & $Codex features enable plugins
    }
}

Write-Step "adding the local marketplace and the plugin"
& $Codex plugin marketplace add $RepoRoot 2>$null | Out-Null
& $Codex plugin add "codex-auto-resume@codex-auto-resume" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Warn "plugin add reported an error; check 'codex plugin list'" }

Write-Step "persisting hook trust"
& $Python $Launcher trust-hooks

Write-Step "installing the per-user helper task"
& $Python $Launcher install-service

Write-Step "doctor"
& $Python $Launcher doctor

Write-Host ""
Write-Host "Done. Use Codex normally: codex"
Write-Host "Status: $Python `"$Launcher`" status"

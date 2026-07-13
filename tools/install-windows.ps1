# One-command Windows installer for RoleNavi.
[CmdletBinding()]
param([switch]$FromCheckout)

$ErrorActionPreference = 'Stop'
$repoUrl = if ($env:ROLENAVI_REPO_URL) { $env:ROLENAVI_REPO_URL } else { 'https://github.com/chriscord/rolenavi.git' }
$installDir = if ($env:ROLENAVI_INSTALL_DIR) { $env:ROLENAVI_INSTALL_DIR } else { Join-Path $HOME 'RoleNavi' }

function Resolve-Git {
    $command = Get-Command git -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $known = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    if (-not (Test-Path -LiteralPath $known)) {
        winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    }
    if (Test-Path -LiteralPath $known) { return $known }
    throw 'Git installation did not complete. Open a new PowerShell window and rerun the command.'
}

function Resolve-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Exe = $launcher.Source; Args = @('-3') }
        }
    }
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $known = Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $known) {
        return [PSCustomObject]@{ Exe = $known; Args = @() }
    }
    throw 'Python installation did not complete. Open a new PowerShell window and rerun the command.'
}

if (-not $FromCheckout) {
    $git = Resolve-Git
    if (Test-Path -LiteralPath $installDir) {
        throw "Install directory already exists: $installDir`nSet ROLENAVI_INSTALL_DIR to choose another location."
    }
    & $git clone --depth 1 $repoUrl $installDir
    & (Join-Path $installDir 'tools\install-windows.ps1') -FromCheckout
    exit $LASTEXITCODE
}

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$python = Resolve-Python
& $python.Exe @($python.Args) -m venv (Join-Path $root '.venv')
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
& $venvPython -m pip install --upgrade pip setuptools
& $venvPython -m pip install -e '.[xlsx]'
& (Join-Path $root '.venv\Scripts\rolenavi.exe') --version

Write-Host ''
Write-Host "RoleNavi installed in $root"
Write-Host '  activate: .\.venv\Scripts\Activate.ps1'
Write-Host '  next:     npm install -g @openai/codex; codex login'
Write-Host '  verify:   rolenavi doctor'

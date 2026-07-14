# One-command Windows installer for RoleNavi.
[CmdletBinding()]
param([switch]$FromCheckout)

$ErrorActionPreference = 'Stop'
$repoUrl = if ($env:ROLENAVI_REPO_URL) { $env:ROLENAVI_REPO_URL } else { 'https://github.com/chriscord/rolenavi.git' }
$callDir = (Get-Location).Path
$installDir = if ($env:ROLENAVI_INSTALL_DIR) { $env:ROLENAVI_INSTALL_DIR } else { Join-Path $callDir 'rolenavi' }

function Assert-NativeSuccess([string]$Action, [int]$ExitCode) {
    if ($ExitCode -ne 0) {
        throw "$Action failed (exit code $ExitCode)."
    }
}

function Resolve-Git {
    $command = Get-Command git -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Git is required, and winget is unavailable. Install Git, open a new PowerShell window, and rerun the command.'
    }
    $knownPaths = @(
        (Join-Path $env:ProgramFiles 'Git\cmd\git.exe'),
        (Join-Path $env:LocalAppData 'Programs\Git\cmd\git.exe')
    )
    if (-not ($knownPaths | Where-Object { Test-Path -LiteralPath $_ })) {
        & $winget.Source install --id Git.Git -e --accept-source-agreements --accept-package-agreements
        Assert-NativeSuccess 'winget Git installation' $LASTEXITCODE
    }
    foreach ($known in $knownPaths) {
        if (Test-Path -LiteralPath $known) { return $known }
    }
    throw 'Git installation did not complete. Open a new PowerShell window and rerun the command.'
}

function Find-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Exe = $launcher.Source; Args = @('-3') }
        }
    }

    foreach ($name in @('python3', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            & $command.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{ Exe = $command.Source; Args = @() }
            }
        }
    }

    $knownPaths = @(
        (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    foreach ($known in $knownPaths) {
        if (Test-Path -LiteralPath $known) {
            & $known -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{ Exe = $known; Args = @() }
            }
        }
    }
    return $null
}

function Resolve-Python {
    $python = Find-Python
    if ($python) { return $python }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'Python 3.10+ is required, and winget is unavailable. Install Python 3.10+, open a new PowerShell window, and rerun the command.'
    }
    & $winget.Source install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    Assert-NativeSuccess 'winget Python installation' $LASTEXITCODE

    $python = Find-Python
    if ($python) { return $python }
    throw 'Python installation did not complete. Open a new PowerShell window and rerun the command.'
}

if ($FromCheckout) {
    $root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
} else {
    $git = Resolve-Git
    if (Test-Path -LiteralPath $installDir) {
        $isLink = (Get-Item -LiteralPath $installDir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint
        $expectedScript = Join-Path $installDir 'tools\install-windows.ps1'
        $origin = if (Test-Path -LiteralPath (Join-Path $installDir '.git')) {
            (& $git -C $installDir remote get-url origin 2>$null)
        } else { $null }
        if ($isLink -or -not (Test-Path -LiteralPath $expectedScript) -or $origin -ne $repoUrl) {
            throw "Install directory exists but is not the expected RoleNavi checkout: $installDir`nMove it aside or set ROLENAVI_INSTALL_DIR to choose another location."
        }
        $trackedChanges = (& $git -C $installDir status --porcelain --untracked-files=no)
        Assert-NativeSuccess 'git status' $LASTEXITCODE
        if ($trackedChanges) {
            throw "The existing RoleNavi checkout has tracked changes: $installDir`nCommit, stash, or discard those changes before rerunning the installer."
        }
        Write-Host "Updating the existing RoleNavi checkout in $installDir"
        & $git -C $installDir pull --ff-only
        Assert-NativeSuccess 'git pull' $LASTEXITCODE
    } else {
        & $git clone --depth 1 $repoUrl $installDir
        Assert-NativeSuccess 'git clone' $LASTEXITCODE
    }
    $root = (Resolve-Path -LiteralPath $installDir).Path
}

$python = Resolve-Python
& $python.Exe @($python.Args) -m venv (Join-Path $root '.venv')
Assert-NativeSuccess 'virtual environment creation' $LASTEXITCODE
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
& $venvPython -m pip install --upgrade pip setuptools
Assert-NativeSuccess 'pip bootstrap' $LASTEXITCODE
& $venvPython -m pip install -e '.[xlsx]'
Assert-NativeSuccess 'RoleNavi installation' $LASTEXITCODE
& (Join-Path $root '.venv\Scripts\rolenavi.exe') --version
Assert-NativeSuccess 'RoleNavi verification' $LASTEXITCODE

Write-Host ''
Write-Host "RoleNavi is ready in $root"
Write-Host "  start:  cd `"$root`"; .\start.cmd"
Write-Host "  check:  cd `"$root`"; .\start.cmd doctor"
Write-Host '  Codex:  npm install -g @openai/codex; codex login'

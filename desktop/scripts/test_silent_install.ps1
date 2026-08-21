# Silent install to a user-writable test directory (no Program Files / less UAC).
# Verifies layout: AICA.exe + engine\, then probes health with env from project .env.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Setup = Join-Path $Root "dist\AICA_Setup_1.0.0.exe"
$InstallDir = Join-Path $env:LOCALAPPDATA "AICA_DesktopTest"
$Port = "18795"

if (-not (Test-Path $Setup)) { throw "Missing installer: $Setup" }

Write-Host "==> Uninstall previous test install if present"
$unins = Join-Path $InstallDir "unins000.exe"
if (Test-Path $unins) {
    Start-Process -FilePath $unins -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES" -Wait -NoNewWindow
}

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
}

Write-Host "==> Silent install -> $InstallDir"
$p = Start-Process -FilePath $Setup -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/DIR=`"$InstallDir`"" -PassThru -Wait
if ($p.ExitCode -ne 0 -and $null -ne $p.ExitCode) {
    Write-Warning "Installer exit code: $($p.ExitCode)"
}

$Launch = Join-Path $InstallDir "AICA.exe"
$Engine = Join-Path $InstallDir "engine\AICA.Engine.exe"
if (-not (Test-Path $Launch)) { throw "Missing launcher after install: $Launch" }
if (-not (Test-Path $Engine)) { throw "Missing engine after install: $Engine" }
$Weights = Join-Path $InstallDir "engine\_internal\vision\weights\aica_product_detector.pt"
if (-not (Test-Path $Weights)) { throw "Missing YOLO weights in install tree" }
Write-Host "OK install layout"

Write-Host "==> Start installed engine (no system Python) with env from project .env"
$py = Join-Path $Root "venv\Scripts\python.exe"
& $py -c @"
from dotenv import dotenv_values
import os, subprocess, time, urllib.request, sys
from pathlib import Path
root = Path(r'$Root')
install = Path(r'$InstallDir')
v = dotenv_values(root / '.env')
env = os.environ.copy()
for k in ('DATABASE_URL', 'GEMINI_API_KEY', 'AICA_SECRET_KEY'):
    if v.get(k):
        env[k] = v[k]
env['AICA_PORT'] = '$Port'
env['AICA_HOST'] = '127.0.0.1'
env['AICA_DESKTOP'] = '1'
exe = install / 'engine' / 'AICA.Engine.exe'
p = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(90):
        time.sleep(2)
        if p.poll() is not None:
            raise SystemExit('installed engine exited early')
        try:
            print(urllib.request.urlopen('http://127.0.0.1:$Port/health', timeout=2).read().decode())
            print('login', urllib.request.urlopen('http://127.0.0.1:$Port/login', timeout=15).status)
            break
        except Exception:
            pass
    else:
        raise SystemExit('health timeout')
finally:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=12)
        except Exception:
            p.kill()
print('installed_engine_ok')
"@

Write-Host "DONE silent install verification"

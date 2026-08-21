# Desktop smoke checks (dev machine). Does not install.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root
$Py = Join-Path $Root "venv\Scripts\python.exe"

Write-Host "==> Web import"
& $Py -c "from backend.main import app; assert any(getattr(r,'path',None)=='/health' for r in app.routes); print('web health route ok')"

Write-Host "==> Artifacts"
$need = @(
  "dist\AICA.exe",
  "dist\AICA.Engine\AICA.Engine.exe",
  "dist\AICA.Engine\_internal\vision\weights\aica_product_detector.pt"
)
foreach ($n in $need) {
  $p = Join-Path $Root $n
  if (-not (Test-Path $p)) { throw "Missing $p" }
  Write-Host "  OK $n"
}

$setup = Join-Path $Root "dist\AICA_Setup_1.0.0.exe"
if (Test-Path $setup) {
  $mb = [math]::Round((Get-Item $setup).Length/1MB,1)
  Write-Host "  OK installer ($mb MB)"
} else {
  Write-Warning "Installer not built yet"
}

Write-Host "==> Packaged engine health (uses process env from project .env; does not write secrets)"
& $Py -c @"
from dotenv import dotenv_values
import os, subprocess, time, urllib.request, sys
from pathlib import Path
root = Path(r'$Root')
v = dotenv_values(root / '.env')
env = os.environ.copy()
for k in ('DATABASE_URL', 'GEMINI_API_KEY', 'AICA_SECRET_KEY'):
    if v.get(k):
        env[k] = v[k]
env['AICA_PORT'] = '18777'
env['AICA_HOST'] = '127.0.0.1'
env['AICA_DESKTOP'] = '1'
exe = root / 'dist' / 'AICA.Engine' / 'AICA.Engine.exe'
p = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(90):
        time.sleep(2)
        if p.poll() is not None:
            print(p.stdout.read().decode('utf-8','replace')[-2000:])
            raise SystemExit('engine exited')
        try:
            print(urllib.request.urlopen('http://127.0.0.1:18777/health', timeout=2).read().decode())
            print('login', urllib.request.urlopen('http://127.0.0.1:18777/login', timeout=15).status)
            break
        except Exception:
            pass
    else:
        raise SystemExit('timeout')
finally:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
print('smoke ok')
"@

Write-Host "DONE"

# Despliega EduMaster en Windows Server (2022/2025) sin Docker: Python + Node + Git. La API FastAPI sirve el
# frontend compilado en 127.0.0.1:8000 y Caddy la publica en 80/443 con HTTPS automático (Let's Encrypt).
# Ejecutar como Administrador (o desde Azure: VM > Ejecutar comando > RunPowerShellScript).
param(
  [string]$Base = "C:\edumaster",
  [string]$BackendRepo = "https://github.com/Seminariosenati/edumaster-backend.git",
  [string]$FrontendRepo = "https://github.com/Seminariosenati/edumaster-frontend.git",
  [string]$Domain = "edumaster-win.northcentralus.cloudapp.azure.com",
  [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-IfMissing($command, $url, $file, $installArgs, $exePath = $null) {
  if ($exePath -and (Test-Path $exePath)) { Write-Host "$command ya instalado"; return }
  if (-not $exePath -and (Get-Command $command -ErrorAction SilentlyContinue)) { Write-Host "$command ya instalado"; return }
  $out = Join-Path $env:TEMP $file
  Write-Host "Descargando $file ..."
  Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
  if ($file.EndsWith(".msi")) { Start-Process msiexec.exe -ArgumentList "/i `"$out`" /qn" -Wait }
  else { Start-Process $out -ArgumentList $installArgs -Wait }
  Refresh-Path
}

function Test-Python($exe) {
  if (-not (Test-Path $exe)) { return $false }
  # En Windows PowerShell, stderr redirigido con ErrorActionPreference=Stop corta el script.
  $ErrorActionPreference = "Continue"
  $out = & $exe -c "import encodings, venv, sys; print('ok')" 2>&1
  $ok = ($LASTEXITCODE -eq 0) -and ("$out".Trim() -eq "ok")
  $ErrorActionPreference = "Stop"
  return $ok
}

# Se valida que Python funcione de verdad: un python.exe sin su carpeta Lib parece instalado pero no arranca.
$py = "C:\Program Files\Python313\python.exe"
$pyInstaller = Join-Path $env:TEMP "python-3.13.1-amd64.exe"
Refresh-Path
if (-not (Test-Python $py)) {
  Stop-ScheduledTask -TaskName "EduMasterAPI" -ErrorAction SilentlyContinue
  Write-Host "Python ausente o dañado: instalación limpia de Python 3.13.1 ..."
  Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe" -OutFile $pyInstaller -UseBasicParsing
  if (Test-Path $py) {
    Start-Process $pyInstaller -ArgumentList "/quiet /uninstall" -Wait
    Remove-Item -Recurse -Force "C:\Program Files\Python313" -ErrorAction SilentlyContinue
  }
  Start-Process $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
  if (-not (Test-Python $py)) {
    # Si el registro aún cree que está instalado, la instalación anterior no hace nada; se fuerza una reparación.
    Start-Process $pyInstaller -ArgumentList "/quiet /repair" -Wait
  }
  Refresh-Path
  if (-not (Test-Python $py)) { throw "Python no funciona después de reinstalarlo; revisar $py" }
} else { Write-Host "python ya instalado" }
Install-IfMissing "node" "https://nodejs.org/dist/v22.12.0/node-v22.12.0-x64.msi" "node-v22.12.0-x64.msi" ""
Install-IfMissing "git" "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" "Git-2.47.1-64-bit.exe" "/VERYSILENT /NORESTART"
Refresh-Path

# PyTorch necesita las DLL de Visual C++ (c10.dll falla al cargar sin ellas).
if (-not (Test-Path "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")) {
  $vc = Join-Path $env:TEMP "vc_redist.x64.exe"
  Write-Host "Instalando Microsoft Visual C++ Redistributable ..."
  Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vc -UseBasicParsing
  Start-Process $vc -ArgumentList "/install /quiet /norestart" -Wait
}

New-Item -ItemType Directory -Force -Path $Base, "$Base\data\models" | Out-Null
foreach ($pair in @(@("edumaster-backend", $BackendRepo), @("edumaster-frontend", $FrontendRepo))) {
  $dir = Join-Path $Base $pair[0]
  if (Test-Path "$dir\.git") { git -C $dir pull --ff-only } else { git clone $pair[1] $dir }
}

$api = "$Base\edumaster-backend\apps\api"
$venv = "$Base\venv"
$venvOk = Test-Python "$venv\Scripts\python.exe"
if (-not $venvOk) {
  Stop-ScheduledTask -TaskName "EduMasterAPI" -ErrorAction SilentlyContinue
  if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
  & $py -m venv $venv
  if (-not (Test-Python "$venv\Scripts\python.exe")) { throw "No se pudo crear el entorno virtual en $venv" }
}
& "$venv\Scripts\python.exe" -m pip install --upgrade pip
& "$venv\Scripts\pip.exe" install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu
& "$venv\Scripts\pip.exe" install -r "$api\requirements.txt"
& "$venv\Scripts\pip.exe" install --no-deps "$Base\edumaster-backend\ml"

Push-Location "$Base\edumaster-frontend"
npm ci
npm run build
Pop-Location

$envFile = "$Base\edumaster.env.ps1"
if (-not (Test-Path $envFile)) {
  $secret = -join ((48..57) + (97..102) | Get-Random -Count 64 | ForEach-Object { [char]$_ })
  @"
`$env:EDUMASTER_ENV = 'production'
`$env:EDUMASTER_DATABASE_URL = 'sqlite:///$($Base.Replace('\','/'))/data/edumaster.db'
`$env:EDUMASTER_MODEL_DIR = '$Base\data\models'
`$env:EDUMASTER_WEB_DIST = '$Base\edumaster-frontend\dist'
`$env:EDUMASTER_JWT_SECRET = '$secret'
`$env:EDUMASTER_SEED_DEMO = 'true'
"@ | Set-Content -Path $envFile -Encoding UTF8
  icacls $envFile /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" | Out-Null
}

@"
. '$envFile'
Set-Location '$api'
& '$venv\Scripts\uvicorn.exe' app.main:app --host 127.0.0.1 --port $Port *>> '$Base\data\api.log'
"@ | Set-Content -Path "$Base\run-api.ps1" -Encoding UTF8

# Caddy termina HTTPS en 80/443 y reenvía a la API local.
New-Item -ItemType Directory -Force -Path "$Base\caddy" | Out-Null
if (-not (Test-Path "$Base\caddy\caddy.exe")) {
  Invoke-WebRequest -Uri "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile "$Base\caddy\caddy.exe" -UseBasicParsing
}
# IP pública desde los metadatos de Azure: permite entrar también por http://<IP> (sin HTTPS, no hay certificados para IPs).
$PublicIp = $null
try {
  $PublicIp = Invoke-RestMethod -Headers @{ Metadata = "true" } -TimeoutSec 5 -Uri "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text"
} catch { Write-Host "No se pudo leer la IP pública; solo habrá acceso por el dominio" }
$ipBlock = ""
if ($PublicIp) {
  $ipBlock = @"

http://$PublicIp {
	encode gzip
	reverse_proxy 127.0.0.1:$Port {
		flush_interval -1
	}
}
"@
}
@"
$Domain {
	encode gzip
	reverse_proxy 127.0.0.1:$Port {
		flush_interval -1
	}
}
$ipBlock
"@ | Set-Content -Path "$Base\caddy\Caddyfile" -Encoding ascii
@"
`$env:XDG_DATA_HOME = '$Base\caddy\data'
`$env:XDG_CONFIG_HOME = '$Base\caddy\config'
& '$Base\caddy\caddy.exe' run --config '$Base\caddy\Caddyfile' *>> '$Base\caddy\caddy.log'
"@ | Set-Content -Path "$Base\run-caddy.ps1" -Encoding UTF8

# IIS ocupa el puerto 80; se detiene su sitio por defecto (se puede reactivar desde el Administrador de IIS).
if (Get-Module -ListAvailable -Name WebAdministration) {
  Import-Module WebAdministration
  if (Get-Website -Name "Default Web Site" -ErrorAction SilentlyContinue) {
    Stop-Website -Name "Default Web Site"
    Set-ItemProperty "IIS:\Sites\Default Web Site" -Name serverAutoStart -Value $false
    Write-Host "Sitio 'Default Web Site' de IIS detenido para liberar el puerto 80"
  }
}

if (-not (Get-NetFirewallRule -DisplayName "EduMaster HTTP/HTTPS" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "EduMaster HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80, 443 -Action Allow | Out-Null
}

$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
foreach ($t in @(@("EduMasterAPI", "run-api.ps1"), @("EduMasterCaddy", "run-caddy.ps1"))) {
  Stop-ScheduledTask -TaskName $t[0] -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $t[0] -Confirm:$false -ErrorAction SilentlyContinue
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Base\$($t[1])`""
  Register-ScheduledTask -TaskName $t[0] -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $settings -User "SYSTEM" -RunLevel Highest | Out-Null
  Start-ScheduledTask -TaskName $t[0]
}

Start-Sleep -Seconds 120
try {
  $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/health"
  Write-Host "EduMaster en marcha: $($health.status). Abre https://$Domain (el certificado tarda ~1 min la primera vez)"
  if ($PublicIp) { Write-Host "También por IP: http://$PublicIp" }
} catch {
  Write-Host "La API aún no responde; revisa $Base\data\api.log"
}

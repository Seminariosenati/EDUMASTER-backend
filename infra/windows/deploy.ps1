# Despliega EduMaster en Windows Server (2022/2025) sin Docker: Python + Node + Git, API FastAPI que sirve
# también el frontend compilado en el puerto 80, registrada como tarea al iniciar el sistema.
# Ejecutar como Administrador (o desde Azure: VM > Ejecutar comando > RunPowerShellScript).
param(
  [string]$Base = "C:\edumaster",
  [string]$BackendRepo = "https://github.com/Seminariosenati/edumaster-backend.git",
  [string]$FrontendRepo = "https://github.com/Seminariosenati/edumaster-frontend.git",
  [int]$Port = 80
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-IfMissing($command, $url, $file, $installArgs) {
  if (Get-Command $command -ErrorAction SilentlyContinue) { Write-Host "$command ya instalado"; return }
  $out = Join-Path $env:TEMP $file
  Write-Host "Descargando $file ..."
  Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
  if ($file.EndsWith(".msi")) { Start-Process msiexec.exe -ArgumentList "/i `"$out`" /qn" -Wait }
  else { Start-Process $out -ArgumentList $installArgs -Wait }
  Refresh-Path
}

Install-IfMissing "python" "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe" "python-3.13.1-amd64.exe" "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
Install-IfMissing "node" "https://nodejs.org/dist/v22.12.0/node-v22.12.0-x64.msi" "node-v22.12.0-x64.msi" ""
Install-IfMissing "git" "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" "Git-2.47.1-64-bit.exe" "/VERYSILENT /NORESTART"
Refresh-Path

New-Item -ItemType Directory -Force -Path $Base, "$Base\data\models" | Out-Null
foreach ($pair in @(@("edumaster-backend", $BackendRepo), @("edumaster-frontend", $FrontendRepo))) {
  $dir = Join-Path $Base $pair[0]
  if (Test-Path "$dir\.git") { git -C $dir pull --ff-only } else { git clone $pair[1] $dir }
}

$api = "$Base\edumaster-backend\apps\api"
$venv = "$Base\venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { python -m venv $venv }
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
& '$venv\Scripts\uvicorn.exe' app.main:app --host 0.0.0.0 --port $Port *>> '$Base\data\api.log'
"@ | Set-Content -Path "$Base\run-api.ps1" -Encoding UTF8

# IIS ocupa el puerto 80; se detiene su sitio por defecto (se puede reactivar desde el Administrador de IIS).
if ($Port -eq 80 -and (Get-Module -ListAvailable -Name WebAdministration)) {
  Import-Module WebAdministration
  if (Get-Website -Name "Default Web Site" -ErrorAction SilentlyContinue) {
    Stop-Website -Name "Default Web Site"
    Set-ItemProperty "IIS:\Sites\Default Web Site" -Name serverAutoStart -Value $false
    Write-Host "Sitio 'Default Web Site' de IIS detenido para liberar el puerto 80"
  }
}

if (-not (Get-NetFirewallRule -DisplayName "EduMaster HTTP" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "EduMaster HTTP" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}

$task = "EduMasterAPI"
Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Base\run-api.ps1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest | Out-Null
Start-ScheduledTask -TaskName $task

Start-Sleep -Seconds 40
try {
  $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/v1/health"
  Write-Host "EduMaster en marcha: $($health.status). Abre http://edumaster-win.northcentralus.cloudapp.azure.com"
} catch {
  Write-Host "La API aún no responde; revisa $Base\data\api.log"
}

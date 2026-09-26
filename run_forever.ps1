<#
.SYNOPSIS
    Supervisor 24/7 del bot QUANT-KALSHI.

.DESCRIPTION
    Mantiene vivo `python main.py` en modo PAPER (dinero simulado) contra el
    entorno de Kalshi detectado. Si el proceso muere o el endpoint /api/health
    deja de responder, lo relanza automaticamente con backoff exponencial.

    Ademas:
      - Redirige stdout/stderr a logs\bot_<fecha>.log
      - Rota y borra logs antiguos (RetentionDays)
      - Detecta una instancia ya activa en el puerto para no duplicar el bot
      - Espera de forma ordenada para poder detenerlo con Ctrl+C o stop_bot.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\run_forever.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$RestartDelaySeconds = 10,
    [int]$HealthIntervalSeconds = 30,
    [int]$RetentionDays = 7,
    [int]$MaxConsecutiveFailures = 12
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$pidFile = Join-Path $logDir "bot.pid"
$supervisorPidFile = Join-Path $logDir "supervisor.pid"
$healthUrl = "http://127.0.0.1:$Port/api/health"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $line = "[$stamp] [$Level] $Message"
    Write-Host $line
    Add-Content -Path (Join-Path $logDir ("supervisor_" + (Get-Date -Format "yyyyMMdd") + ".log")) -Value $line
}

function Remove-OldLogs {
    try {
        Get-ChildItem -Path $logDir -Filter "*.log" -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$RetentionDays) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

function Test-PortInUse {
    param([int]$LocalPort)
    try {
        $listener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
        return [bool]$listener
    } catch {
        return $false
    }
}

function Test-BotHealthy {
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 15 -ErrorAction Stop
        if ($null -eq $resp) { return $false }
        # Cualquier respuesta valida indica que el proceso esta vivo.
        return $true
    } catch {
        return $false
    }
}

# Evitar dos supervisores simultaneos.
if (Test-Path $supervisorPidFile) {
    $oldPid = Get-Content $supervisorPidFile -ErrorAction SilentlyContinue
    if ($oldPid) {
        $existing = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($existing) {
            Write-Log "Ya hay un supervisor activo (PID $oldPid). Saliendo." "WARN"
            exit 0
        }
    }
}
Set-Content -Path $supervisorPidFile -Value $PID

Write-Log "Supervisor iniciado. Cwd=$root Puerto=$Port Modo=PAPER"

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Log "No se encontro .env: el cliente arrancara sin credenciales (solo datos publicos)." "WARN"
}

$consecutiveFailures = 0
$restartCount = 0

while ($true) {
    Remove-OldLogs

    # Si el puerto ya esta ocupado, no arrancar otra copia.
    if (Test-PortInUse -LocalPort $Port) {
        if (Test-BotHealthy) {
            Write-Log "El bot ya esta respondiendo en $healthUrl. Supervisor en modo vigilancia."
            while (Test-BotHealthy) {
                Start-Sleep -Seconds $HealthIntervalSeconds
            }
            Write-Log "El bot dejo de responder. Se asume caida y se relanza." "WARN"
        } else {
            Write-Log "Puerto $Port ocupado por un proceso que no responde a /api/health. Esperando..." "WARN"
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }
    }

    $logFile = Join-Path $logDir ("bot_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
    Write-Log "Arrancando bot (intento $($restartCount + 1)). Log: $(Split-Path -Leaf $logFile)"

    $proc = $null
    try {
        $proc = Start-Process -FilePath "python" `
            -ArgumentList "main.py" `
            -WorkingDirectory $root `
            -RedirectStandardOutput $logFile `
            -RedirectStandardError ($logFile -replace "\.log$", ".err.log") `
            -PassThru -WindowStyle Hidden
        Set-Content -Path $pidFile -Value $proc.Id
        Write-Log "Bot arrancado con PID $($proc.Id)"
    } catch {
        Write-Log "Fallo al arrancar python main.py: $_" "ERROR"
        $restartCount++
        Start-Sleep -Seconds $RestartDelaySeconds
        continue
    }

    # Vigilar mientras el proceso viva.
    $wasHealthy = $false
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds $HealthIntervalSeconds
        $proc.Refresh()
        if ($proc.HasExited) { break }

        if (Test-BotHealthy) {
            if (-not $wasHealthy) {
                Write-Log "Healthcheck OK: el bot esta sirviendo datos."
                $wasHealthy = $true
                $consecutiveFailures = 0
            } else {
                Write-Log "Healthcheck OK (sigue vivo)."
            }
        } else {
            Write-Log "Healthcheck fallido: /api/health no responde todavia." "WARN"
        }
        Remove-OldLogs
    }

    $exitCode = $null
    try { $proc.Refresh(); $exitCode = $proc.ExitCode } catch { }

    if ($wasHealthy) {
        $consecutiveFailures = 0
    } else {
        $consecutiveFailures++
    }
    $restartCount++
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue

    $delay = [Math]::Min(300, $RestartDelaySeconds * [Math]::Pow(2, [Math]::Min($consecutiveFailures, 5)))
    Write-Log "El bot termino (exit=$exitCode). Fallos consecutivos sin health=$consecutiveFailures. Reintento en $([int]$delay)s." "WARN"

    if ($consecutiveFailures -ge $MaxConsecutiveFailures) {
        Write-Log "Demasiados fallos consecutivos ($consecutiveFailures). Revisa logs\bot_*.err.log. Deteniendo supervisor." "ERROR"
        break
    }

    Start-Sleep -Seconds ([int]$delay)
}

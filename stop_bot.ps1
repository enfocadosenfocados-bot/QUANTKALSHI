<#
.SYNOPSIS
    Detiene ordenadamente el bot QUANT-KALSHI y su supervisor.

.DESCRIPTION
    1. Detiene la tarea programada (si esta registrada)
    2. Finaliza el supervisor (logs\supervisor.pid)
    3. Finaliza el proceso del bot (logs\bot.pid)
    4. Verifica que el puerto quede libre

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\stop_bot.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = "QuantKalshiBot",
    [int]$Port = 8000
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
$pidFile = Join-Path $logDir "bot.pid"
$supervisorPidFile = Join-Path $logDir "supervisor.pid"

function Stop-PidFile {
    param([string]$File, [string]$Label)
    if (-not (Test-Path $File)) { return }
    $value = Get-Content $File -ErrorAction SilentlyContinue
    if ($value) {
        $proc = Get-Process -Id $value -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $value -Force -ErrorAction SilentlyContinue
            Write-Host "  $Label detenido (PID $value)." -ForegroundColor Yellow
        }
    }
    Remove-Item $File -Force -ErrorAction SilentlyContinue
}

Write-Host "Deteniendo QUANT-KALSHI..." -ForegroundColor Cyan

# 1) Tarea programada primero: evita que el scheduler relance el supervisor.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "  Tarea '$TaskName' detenida." -ForegroundColor Yellow
}

Stop-PidFile -File $supervisorPidFile -Label "Supervisor"
Stop-PidFile -File $pidFile -Label "Bot"

# 2) Restos: procesos python que sirvan este proyecto.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*main.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  Proceso python main.py detenido (PID $($_.ProcessId))." -ForegroundColor Yellow
    }

Start-Sleep -Seconds 2
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Host "El puerto $Port sigue ocupado. Revisa con: Get-NetTCPConnection -LocalPort $Port" -ForegroundColor Red
} else {
    Write-Host "Bot detenido y puerto $Port libre." -ForegroundColor Green
}

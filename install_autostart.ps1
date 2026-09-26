<#
.SYNOPSIS
    Configura el arranque automatico del bot QUANT-KALSHI para operar 24/7.

.DESCRIPTION
    Intenta, en este orden:

      1) Tarea programada de Windows "QuantKalshiBot" (lo mas robusto: se
         reinicia sola si falla). Requiere permisos de administrador.
      2) Carpeta de Inicio del usuario (NO requiere admin): deja un lanzador
         oculto que arranca el supervisor al iniciar sesion.

    En ambos casos el verdadero guardian es run_forever.ps1, que relanza el bot
    si el proceso muere o si /api/health deja de responder.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Uninstall
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Method Task
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Method Startup
#>
[CmdletBinding()]
param(
    [string]$TaskName = "QuantKalshiBot",
    [ValidateSet("Auto", "Task", "Startup")]
    [string]$Method = "Auto",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$supervisor = Join-Path $root "run_forever.ps1"
$startupDir = [Environment]::GetFolderPath("Startup")
$launcherPath = Join-Path $startupDir "QuantKalshiBot.vbs"

function Remove-StartupLauncher {
    if (Test-Path $launcherPath) {
        Remove-Item $launcherPath -Force
        Write-Host "  Lanzador de Inicio eliminado." -ForegroundColor Yellow
    }
}

function Remove-TaskIfExists {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "  Tarea '$TaskName' eliminada." -ForegroundColor Yellow
    }
}

if ($Uninstall) {
    Remove-TaskIfExists
    Remove-StartupLauncher
    Write-Host "Arranque automatico desactivado." -ForegroundColor Green
    return
}

if (-not (Test-Path $supervisor)) {
    throw "No se encontro run_forever.ps1 en $root"
}

function Install-Task {
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $supervisor) `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Force `
        -Description "Supervisor 24/7 del bot QUANT-KALSHI (modo PAPER, exchange Kalshi)." | Out-Null
    Start-ScheduledTask -TaskName $TaskName
}

function Install-StartupLauncher {
    # Lanzador VBS: arranca el supervisor sin ventana visible al iniciar sesion.
    $vbs = @(
        'Set shell = CreateObject("WScript.Shell")',
        ('shell.CurrentDirectory = "{0}"' -f $root),
        ('shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{0}""", 0, False' -f $supervisor)
    ) -join "`r`n"
    Set-Content -Path $launcherPath -Value $vbs -Encoding ASCII
}

$installedTask = $false
if ($Method -in @("Auto", "Task")) {
    try {
        Install-Task
        $installedTask = $true
        Write-Host "Tarea programada '$TaskName' registrada (se reinicia sola si falla)." -ForegroundColor Green
    } catch {
        if ($Method -eq "Task") { throw }
        Write-Host "No se pudo registrar la tarea programada (requiere administrador)." -ForegroundColor Yellow
        Write-Host "Se usara la carpeta de Inicio como alternativa." -ForegroundColor Yellow
    }
}

if (-not $installedTask) {
    Install-StartupLauncher
    Write-Host "Lanzador de Inicio creado en:" -ForegroundColor Green
    Write-Host "  $launcherPath" -ForegroundColor Cyan
    Write-Host "Arrancara automaticamente al iniciar sesion en Windows." -ForegroundColor Green
}

Write-Host ""
Write-Host "Dashboard: http://localhost:8000/dashboard" -ForegroundColor Cyan
Write-Host "Estado:    http://localhost:8000/api/health" -ForegroundColor Cyan
Write-Host "Detener:   powershell -ExecutionPolicy Bypass -File .\stop_bot.ps1" -ForegroundColor Cyan
Write-Host "Logs:      $root\logs" -ForegroundColor Cyan

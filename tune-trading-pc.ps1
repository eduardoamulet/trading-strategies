# Trading PC Tune-Up Script
# For TradingView + thinkorswim on Windows 11
# Run in an ELEVATED PowerShell (Right-click -> Run as Administrator)

#Requires -RunAsAdministrator

Write-Host "=== Trading PC Tune-Up ===" -ForegroundColor Cyan
Write-Host ""

# ---------- 1. POWER PLAN ----------
Write-Host "[1/6] Setting High Performance power plan..." -ForegroundColor Yellow
try {
    # Activate Ultimate Performance (unhide first), fallback to High Performance
    powercfg -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61 2>$null | Out-Null
    $ultimate = (powercfg /list | Select-String "Ultimate Performance")
    if ($ultimate) {
        $guid = ($ultimate -split ' ')[3]
        powercfg /setactive $guid
        Write-Host "    Ultimate Performance plan activated." -ForegroundColor Green
    } else {
        powercfg /setactive SCHEME_MIN  # High Performance
        Write-Host "    High Performance plan activated." -ForegroundColor Green
    }

    # Disable CPU throttling on AC
    powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN 100
    powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMAX 100
    # Keep disks awake
    powercfg /setacvalueindex SCHEME_CURRENT SUB_DISK DISKIDLE 0
    # Disable USB selective suspend (helps with data-feed dongles/mice)
    powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    powercfg /setactive SCHEME_CURRENT
    Write-Host "    CPU/Disk/USB power tweaks applied." -ForegroundColor Green
} catch {
    Write-Host "    Power plan tweak failed: $_" -ForegroundColor Red
}

# ---------- 2. GPU PREFERENCE (High Performance for trading apps) ----------
Write-Host ""
Write-Host "[2/6] Setting GPU preference to High Performance for TOS & TradingView..." -ForegroundColor Yellow
$gpuKey = "HKCU:\SOFTWARE\Microsoft\DirectX\UserGpuPreferences"
if (!(Test-Path $gpuKey)) { New-Item -Path $gpuKey -Force | Out-Null }

$apps = @()
# Find thinkorswim.exe
$tosPaths = @(
    "$env:LOCALAPPDATA\thinkorswim\thinkorswim.exe",
    "C:\Program Files\thinkorswim\thinkorswim.exe",
    "${env:ProgramFiles(x86)}\thinkorswim\thinkorswim.exe"
)
foreach ($p in $tosPaths) { if (Test-Path $p) { $apps += $p } }

# Find TradingView.exe
$tvPaths = @(
    "$env:LOCALAPPDATA\Programs\TradingView\TradingView.exe",
    "$env:LOCALAPPDATA\TradingView\TradingView.exe"
)
foreach ($p in $tvPaths) { if (Test-Path $p) { $apps += $p } }

if ($apps.Count -eq 0) {
    Write-Host "    No TOS/TradingView executables found. Skipping (apps may not be installed in default paths)." -ForegroundColor DarkYellow
} else {
    foreach ($app in $apps) {
        Set-ItemProperty -Path $gpuKey -Name $app -Value "GpuPreference=2;" -Force
        Write-Host "    GPU = High Performance -> $app" -ForegroundColor Green
    }
}

# ---------- 3. WINDOWS DEFENDER EXCLUSIONS ----------
Write-Host ""
Write-Host "[3/6] Adding Defender exclusions for trading apps..." -ForegroundColor Yellow
try {
    $exclusions = @(
        "$env:LOCALAPPDATA\thinkorswim",
        "C:\Program Files\thinkorswim",
        "$env:LOCALAPPDATA\Programs\TradingView",
        "$env:LOCALAPPDATA\TradingView"
    ) | Where-Object { Test-Path $_ }

    foreach ($e in $exclusions) {
        Add-MpPreference -ExclusionPath $e -ErrorAction SilentlyContinue
        Write-Host "    Excluded: $e" -ForegroundColor Green
    }

    $exeExclusions = @("thinkorswim.exe", "TradingView.exe", "java.exe", "javaw.exe")
    foreach ($e in $exeExclusions) {
        Add-MpPreference -ExclusionProcess $e -ErrorAction SilentlyContinue
    }
    Write-Host "    Process exclusions added." -ForegroundColor Green
} catch {
    Write-Host "    Defender exclusion failed: $_" -ForegroundColor Red
}

# ---------- 4. NETWORK TWEAKS ----------
Write-Host ""
Write-Host "[4/6] Applying network tweaks for low-latency data feeds..." -ForegroundColor Yellow
try {
    netsh int tcp set global autotuninglevel=normal | Out-Null
    netsh int tcp set global rss=enabled | Out-Null
    netsh int tcp set global ecncapability=enabled | Out-Null
    # Disable Nagle's algorithm on active interfaces (reduces small-packet latency)
    $ifaces = Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    foreach ($i in $ifaces) {
        Set-ItemProperty -Path $i.PSPath -Name "TcpAckFrequency" -Value 1 -Type DWord -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $i.PSPath -Name "TCPNoDelay" -Value 1 -Type DWord -ErrorAction SilentlyContinue
        Set-ItemProperty -Path $i.PSPath -Name "TcpDelAckTicks" -Value 0 -Type DWord -ErrorAction SilentlyContinue
    }
    Write-Host "    TCP auto-tuning + Nagle disabled." -ForegroundColor Green
} catch {
    Write-Host "    Network tweak failed: $_" -ForegroundColor Red
}

# ---------- 5. VISUAL EFFECTS: PERFORMANCE ----------
Write-Host ""
Write-Host "[5/6] Optimizing visual effects for performance..." -ForegroundColor Yellow
try {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" -Name "VisualFXSetting" -Value 2 -Type DWord
    # Turn off transparency (less GPU overhead)
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" -Name "EnableTransparency" -Value 0 -Type DWord
    # Disable animations
    Set-ItemProperty -Path "HKCU:\Control Panel\Desktop\WindowMetrics" -Name "MinAnimate" -Value "0" -Type String
    Write-Host "    Visual effects tuned." -ForegroundColor Green
} catch {
    Write-Host "    Visual effect tweak failed: $_" -ForegroundColor Red
}

# ---------- 6. GAME MODE / GAME BAR OFF (avoids window deprioritization) ----------
Write-Host ""
Write-Host "[6/6] Disabling Game Mode & Game Bar..." -ForegroundColor Yellow
try {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\GameBar" -Name "AutoGameModeEnabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\GameBar" -Name "AllowAutoGameMode" -Value 0 -Type DWord -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKCU:\System\GameConfigStore" -Name "GameDVR_Enabled" -Value 0 -Type DWord -ErrorAction SilentlyContinue
    Write-Host "    Game Mode disabled." -ForegroundColor Green
} catch {
    Write-Host "    Game Mode tweak failed: $_" -ForegroundColor Red
}

# ---------- THINKORSWIM JVM HINT ----------
Write-Host ""
Write-Host "=== Manual step for thinkorswim ===" -ForegroundColor Cyan
Write-Host "Edit (or create) this file to raise Java heap to 8GB:"
Write-Host "  $env:LOCALAPPDATA\thinkorswim\thinkorswim.vmoptions" -ForegroundColor White
Write-Host "Add these lines:"
Write-Host "  -Xmx8192m"
Write-Host "  -Xms2048m"
Write-Host "  -XX:+UseG1GC"
Write-Host "  -XX:MaxGCPauseMillis=100"
Write-Host ""
Write-Host "Done. Reboot recommended." -ForegroundColor Green

param(
    [int]$ApiPort = 8765,
    [int]$UiPort = 1420
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $repoRoot "desktop"
$uv = Get-Command uv -ErrorAction Stop
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) {
    $npm = Get-Command npm -ErrorAction Stop
}

# The API stays on loopback and is stopped when Vite exits.
$apiArguments = @("run", "repopilot-guard", "api", "serve", "--host", "127.0.0.1", "--port", "$ApiPort")
$apiLog = Join-Path $env:TEMP "repopilot-api-preview.log"
$apiErrorLog = Join-Path $env:TEMP "repopilot-api-preview.err.log"
if ($ApiPort -eq $UiPort) {
    throw "RepoPilot preview requires distinct API and UI ports."
}
if (Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1) {
    throw ("RepoPilot preview refused to start because port 127.0.0.1:{0} is already in use." -f $ApiPort)
}
if (Get-NetTCPConnection -LocalPort $UiPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1) {
    throw ("RepoPilot preview refused to start because port 127.0.0.1:{0} is already in use." -f $UiPort)
}

function Test-RepoPilotHealth([int]$Port) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:${Port}/api/health" -Method Get -TimeoutSec 2
        return $health.status -eq "READY" -and $health.scope -eq "127.0.0.1-only"
    }
    catch {
        return $false
    }
}

Remove-Item -LiteralPath $apiLog, $apiErrorLog -Force -ErrorAction SilentlyContinue
$env:REPOPILOT_DESKTOP_PREVIEW_ORIGIN = "http://127.0.0.1:${UiPort}"
$api = Start-Process -FilePath $uv.Source -ArgumentList $apiArguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -PassThru

try {
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-RepoPilotHealth -Port $ApiPort) {
            $apiReady = $true
            break
        }
        if ($api.HasExited) { break }
    }
    if (-not $apiReady) {
        $details = ""
        if (Test-Path -LiteralPath $apiErrorLog) { $details = Get-Content -LiteralPath $apiErrorLog -Raw }
        if (-not $details -and (Test-Path -LiteralPath $apiLog)) { $details = Get-Content -LiteralPath $apiLog -Raw }
        throw "RepoPilot API did not start on 127.0.0.1:${ApiPort}. ${details}"
    }
    Write-Host "RepoPilot preview is ready. API: http://127.0.0.1:${ApiPort}; UI: http://127.0.0.1:${UiPort}"
    Push-Location $desktopRoot
    try {
        # 仅将本轮预览启动的 loopback API 地址注入 Vite；正式 Tauri 构建仍使用默认 8765。
        $env:VITE_REPOPILOT_API_URL = "http://127.0.0.1:${ApiPort}/api"
        & $npm.Source run dev -- --host 127.0.0.1 --port $UiPort --open
    }
    finally {
        Pop-Location
    }
}
finally {
    if (-not $api.HasExited) {
        Stop-Process -Id $api.Id -Force
    }
    Remove-Item -LiteralPath $apiLog, $apiErrorLog -Force -ErrorAction SilentlyContinue
}

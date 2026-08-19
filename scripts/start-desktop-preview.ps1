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
$viteLog = Join-Path $env:TEMP "repopilot-vite-preview.log"
$viteErrorLog = Join-Path $env:TEMP "repopilot-vite-preview.err.log"
$healthDiagnostic = ""
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
        # A blocked Agent dependency is distinct from an unavailable loopback API.
        # Validate the HTTP response and JSON contract explicitly for stable preview startup.
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:${Port}/api/health" -Method Get -UseBasicParsing -TimeoutSec 8
        if ($response.StatusCode -ne 200 -or -not $response.Content) {
            $script:healthDiagnostic = "Health endpoint did not return HTTP 200."
            return $false
        }
        $health = $response.Content | ConvertFrom-Json -ErrorAction Stop
        if ($health.status -ne "READY") {
            $script:healthDiagnostic = "Health endpoint did not satisfy the loopback API contract."
            return $false
        }
        if ($health.scope -ne "127.0.0.1-only") {
            $script:healthDiagnostic = "Health endpoint did not satisfy the loopback API contract."
            return $false
        }
        return $true
    }
    catch {
        $script:healthDiagnostic = $_.Exception.Message
        return $false
    }
}

function Stop-RepoPilotProcessTree([int]$ProcessId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-RepoPilotProcessTree -ProcessId $child.ProcessId
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $apiLog, $apiErrorLog, $viteLog, $viteErrorLog -Force -ErrorAction SilentlyContinue
$env:REPOPILOT_DESKTOP_PREVIEW_ORIGIN = "http://127.0.0.1:${UiPort}"
$api = Start-Process -FilePath $uv.Source -ArgumentList $apiArguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -PassThru
$vite = $null

try {
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 8; $attempt++) {
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
        throw "RepoPilot API did not start on 127.0.0.1:${ApiPort}. ${healthDiagnostic} ${details}"
    }
    # Inject only this preview's loopback API into Vite; Tauri keeps its default port.
    $env:VITE_REPOPILOT_API_URL = "http://127.0.0.1:${ApiPort}/api"
    $viteArguments = @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$UiPort")
    $vite = Start-Process -FilePath $npm.Source -ArgumentList $viteArguments -WorkingDirectory $desktopRoot -WindowStyle Hidden -RedirectStandardOutput $viteLog -RedirectStandardError $viteErrorLog -PassThru
    Start-Sleep -Seconds 2
    if ($vite.HasExited) {
        $details = ""
        if (Test-Path -LiteralPath $viteErrorLog) { $details = Get-Content -LiteralPath $viteErrorLog -Raw }
        if (-not $details -and (Test-Path -LiteralPath $viteLog)) { $details = Get-Content -LiteralPath $viteLog -Raw }
        throw "RepoPilot Vite preview did not start on 127.0.0.1:${UiPort}. ${details}"
    }
    Write-Host "RepoPilot preview is ready. API: http://127.0.0.1:${ApiPort}; UI: http://127.0.0.1:${UiPort}"
    Wait-Process -Id $vite.Id
}
finally {
    if ($vite -and -not $vite.HasExited) { Stop-RepoPilotProcessTree -ProcessId $vite.Id }
    if (-not $api.HasExited) { Stop-RepoPilotProcessTree -ProcessId $api.Id }
    Remove-Item -LiteralPath $apiLog, $apiErrorLog, $viteLog, $viteErrorLog -Force -ErrorAction SilentlyContinue
}

param(
    [int]$ApiPort = 8765
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
Remove-Item -LiteralPath $apiLog, $apiErrorLog -Force -ErrorAction SilentlyContinue
$api = Start-Process -FilePath $uv.Source -ArgumentList $apiArguments -WorkingDirectory $repoRoot -WindowStyle Hidden -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -PassThru

try {
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-NetConnection -ComputerName "127.0.0.1" -Port $ApiPort -InformationLevel Quiet) {
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
    Write-Host "RepoPilot preview is ready. API: http://127.0.0.1:${ApiPort}; UI: http://127.0.0.1:1420"
    Push-Location $desktopRoot
    try {
        & $npm.Source run dev -- --host 127.0.0.1 --open
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

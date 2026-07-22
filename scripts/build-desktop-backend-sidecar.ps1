[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uv = Get-Command uv -ErrorAction Stop
$binaryRoot = Join-Path $repoRoot "desktop\src-tauri\binaries"
$buildRoot = Join-Path $repoRoot ".build-artifacts\desktop-sidecar"
$entrypoint = Join-Path $repoRoot "scripts\repopilot_sidecar.py"
$target = Join-Path $binaryRoot "repopilot-guard.exe"

if ($Clean) {
    Remove-Item -LiteralPath $binaryRoot, $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $binaryRoot, $buildRoot -Force | Out-Null
Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue

# Package only the RepoPilot Python backend. Secrets and user data stay outside.
$pyInstallerArguments = @(
    'run', '--with', 'pyinstaller', '--with', 'mcp[cli]', 'pyinstaller',
    '--noconfirm', '--clean', '--onefile',
    '--name', 'repopilot-guard',
    '--distpath', $binaryRoot,
    '--workpath', (Join-Path $buildRoot 'work'),
    '--specpath', (Join-Path $buildRoot 'spec'),
    '--paths', (Join-Path $repoRoot 'src'),
    '--collect-all', 'langchain',
    '--collect-all', 'langchain_openai',
    '--collect-all', 'langchain_qdrant',
    '--collect-all', 'langgraph',
    '--collect-all', 'mcp',
    '--collect-all', 'qdrant_client',
    $entrypoint
)

& $uv.Source @pyInstallerArguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "RepoPilot Agent sidecar build failed."
}

Write-Host "RepoPilot Agent sidecar created: $target"

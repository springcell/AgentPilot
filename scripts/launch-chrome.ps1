# Launch Chrome with CDP debug port
# Uses separate user-data-dir to avoid conflict with existing Chrome

param(
    [int]$Port = 9222,
    [string]$ChromePath = "",
    [string]$UserDataDir = ""
)

$defaultPaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

$exe = $ChromePath
if (-not $exe -or -not (Test-Path $exe)) {
    foreach ($p in $defaultPaths) {
        if (Test-Path $p) {
            $exe = $p
            break
        }
    }
}

if (-not $exe) {
    Write-Host "Chrome not found. Set llm.cdp.chromePath in config.json" -ForegroundColor Red
    exit 1
}

if (-not $UserDataDir) {
    $UserDataDir = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "chrome-debug-profile"
}
if (-not (Test-Path $UserDataDir)) {
    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
}

Write-Host "Starting Chrome (CDP port: $Port, profile: $UserDataDir)..." -ForegroundColor Cyan
Write-Host "Open https://chatgpt.com/ and login" -ForegroundColor Yellow
$chromeArgs = @(
    ('--remote-debugging-port=' + $Port),
    ('--user-data-dir=' + $UserDataDir)
)
Start-Process -FilePath $exe -ArgumentList $chromeArgs -PassThru

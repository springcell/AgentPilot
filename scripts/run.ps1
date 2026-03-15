
# One-shot: 1 Chrome  2 API  3 Agent
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Message = @()
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
$configPath = Join-Path $rootDir "config.json"

$config = @{}
if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
}

$cdpUrl = "http://127.0.0.1:9222"
if ($config.llm -and $config.llm.cdp -and $config.llm.cdp.url) {
    $cdpUrl = $config.llm.cdp.url
}

$port = 9222
if ($cdpUrl -match ':(\d+)$') { $port = [int]$Matches[1] }

$apiPort = 3000
$apiModelsUrl = "http://127.0.0.1:$apiPort/v1/models"

function Test-CdpReady {
    try {
        $null = Invoke-WebRequest -Uri "$cdpUrl/json/version" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Test-ApiReady {
    try {
        $r = Invoke-WebRequest -Uri $apiModelsUrl -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

# 1. Chrome
if (-not (Test-CdpReady)) {
    Write-Host "[1/3] Starting Chrome..." -ForegroundColor Cyan
    $chromePath = ""
    if ($config.llm -and $config.llm.cdp -and $config.llm.cdp.chromePath) {
        $chromePath = $config.llm.cdp.chromePath
    }
    & (Join-Path $scriptDir "launch-chrome.ps1") -Port $port -ChromePath $chromePath

    Write-Host "Waiting for Chrome CDP (max 30s)..." -ForegroundColor Yellow
    $maxWait = 30
    $waited = 0
    while (-not (Test-CdpReady) -and $waited -lt $maxWait) {
        Start-Sleep -Seconds 2
        $waited += 2
    }

    if (-not (Test-CdpReady)) {
        Write-Host "CDP not ready. Close all Chrome windows and retry, or open https://chatgpt.com/ and log in" -ForegroundColor Red
        exit 1
    }
    Write-Host "Chrome connected ($cdpUrl)" -ForegroundColor Green
} else {
    Write-Host "[1/3] Chrome connected ($cdpUrl)" -ForegroundColor Green
}

# 2. API
if (-not (Test-ApiReady)) {
    Write-Host "[2/3] Starting API..." -ForegroundColor Cyan
    $apiPath = Join-Path $rootDir "src\api-server.js"
    $null = Start-Process -FilePath "node" -ArgumentList $apiPath -WorkingDirectory $rootDir -PassThru -WindowStyle Hidden
    Write-Host "Waiting for API (max 15s)..." -ForegroundColor Yellow
    $waited = 0
    while (-not (Test-ApiReady) -and $waited -lt 15) {
        Start-Sleep -Seconds 1
        $waited++
    }
    if (-not (Test-ApiReady)) {
        Write-Host "API startup timeout. Run: npm run api" -ForegroundColor Red
        exit 1
    }
    Write-Host "API ready (http://127.0.0.1:$apiPort)" -ForegroundColor Green
} else {
    Write-Host "[2/3] API running (http://127.0.0.1:$apiPort)" -ForegroundColor Green
}

# 3. Agent
Write-Host "[3/3] Starting agent..." -ForegroundColor Cyan
& (Join-Path $scriptDir "run-agent.ps1") @Message

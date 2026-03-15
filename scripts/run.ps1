
# 一键启动：1 Chrome  2 API  3 Agent
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

# ── 1. Chrome ─────────────────────────────────────────────
if (-not (Test-CdpReady)) {
    Write-Host "[1/3] 启动 Chrome..." -ForegroundColor Cyan
    $chromePath = ""
    if ($config.llm -and $config.llm.cdp -and $config.llm.cdp.chromePath) {
        $chromePath = $config.llm.cdp.chromePath
    }
    & (Join-Path $scriptDir "launch-chrome.ps1") -Port $port -ChromePath $chromePath

    Write-Host "等待 Chrome CDP (最多 30s)..." -ForegroundColor Yellow
    $maxWait = 30
    $waited = 0
    while (-not (Test-CdpReady) -and $waited -lt $maxWait) {
        Start-Sleep -Seconds 2
        $waited += 2
    }

    if (-not (Test-CdpReady)) {
        Write-Host "CDP 未就绪。请关闭所有 Chrome 窗口后重试，或在浏览器中打开 https://chatgpt.com/ 并登录" -ForegroundColor Red
        exit 1
    }
    Write-Host "Chrome 已连接 ($cdpUrl)" -ForegroundColor Green
} else {
    Write-Host "[1/3] Chrome 已连接 ($cdpUrl)" -ForegroundColor Green
}

# ── 2. API ─────────────────────────────────────────────────
if (-not (Test-ApiReady)) {
    Write-Host "[2/3] 启动 API 服务..." -ForegroundColor Cyan
    $apiPath = Join-Path $rootDir "src\api-server.js"
    $null = Start-Process -FilePath "node" -ArgumentList $apiPath -WorkingDirectory $rootDir -PassThru -WindowStyle Hidden
    Write-Host "等待 API 就绪 (最多 15s)..." -ForegroundColor Yellow
    $waited = 0
    while (-not (Test-ApiReady) -and $waited -lt 15) {
        Start-Sleep -Seconds 1
        $waited++
    }
    if (-not (Test-ApiReady)) {
        Write-Host "API 启动超时。请手动运行: npm run api" -ForegroundColor Red
        exit 1
    }
    Write-Host "API 已就绪 (http://127.0.0.1:$apiPort)" -ForegroundColor Green
} else {
    Write-Host "[2/3] API 已运行 (http://127.0.0.1:$apiPort)" -ForegroundColor Green
}

# ── 3. Agent ───────────────────────────────────────────────
Write-Host "[3/3] 启动智能体..." -ForegroundColor Cyan
& (Join-Path $scriptDir "run-agent.ps1") @Message

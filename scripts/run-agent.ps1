# 运行 Python 智能体：自动检测并启动桥接 API（若未运行）
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$Task = @()
)

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$rootDir = if ($scriptDir) { (Split-Path -Parent $scriptDir) } else { (Get-Location).Path }
$apiPort = 3000
$chatUrl = "http://127.0.0.1:$apiPort/chat"

function Test-ApiReady {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/v1/models" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

# 1. 若 API 未运行，后台启动
if (-not (Test-ApiReady)) {
    Write-Host "桥接 API 未运行，正在启动 api-server..." -ForegroundColor Yellow
    $apiPath = Join-Path $rootDir "src\api-server.js"
    $apiJob = Start-Process -FilePath "node" -ArgumentList $apiPath -WorkingDirectory $rootDir -PassThru -WindowStyle Hidden
    Write-Host "等待 API 就绪 (max 15s)..." -ForegroundColor Yellow
    $waited = 0
    while (-not (Test-ApiReady) -and $waited -lt 15) {
        Start-Sleep -Seconds 1
        $waited++
    }
    if (-not (Test-ApiReady)) {
        Write-Host "API 启动超时。请手动运行: npm run api" -ForegroundColor Red
        exit 1
    }
    Write-Host "桥接 API 已就绪 ($chatUrl)" -ForegroundColor Green
} else {
    Write-Host "桥接 API 已运行 ($chatUrl)" -ForegroundColor Green
}

# 2. 检查 CDP（Chrome）
$cdpUrl = "http://127.0.0.1:9222"
if ($rootDir) {
  $configPath = Join-Path $rootDir "config.json"
  if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    if ($config.llm -and $config.llm.cdp -and $config.llm.cdp.url) {
        $cdpUrl = $config.llm.cdp.url
    }
  }
}
try {
    $null = Invoke-WebRequest -Uri "$cdpUrl/json/version" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
} catch {
    Write-Host ""
    Write-Host "提示: Chrome 未连接。请另开终端运行: npm run chrome" -ForegroundColor Yellow
    Write-Host "      然后在浏览器中登录 https://chatgpt.com/" -ForegroundColor Yellow
    Write-Host ""
}

# 3. 运行 Python 智能体
if (-not $rootDir) { $rootDir = (Get-Location).Path }
$agentPath = Join-Path $rootDir "agent\agent_loop.py"
if (-not (Test-Path $agentPath)) {
    Write-Host "未找到 agent_loop.py: $agentPath" -ForegroundColor Red
    exit 1
}
Set-Location $rootDir
if ($Task.Count -gt 0) {
    $taskStr = $Task -join " "
    & python $agentPath $taskStr
} else {
    & python $agentPath
}

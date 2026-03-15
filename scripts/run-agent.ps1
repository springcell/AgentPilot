# Run Python agent; start bridge API if not running
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

# 1. Start API if not running
if (-not (Test-ApiReady)) {
    Write-Host "Bridge API not running, starting api-server..." -ForegroundColor Yellow
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
    Write-Host "Bridge API ready ($chatUrl)" -ForegroundColor Green
} else {
    Write-Host "Bridge API running ($chatUrl)" -ForegroundColor Green
}

# 2. Check CDP (Chrome)
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
    Write-Host "Chrome not connected. Run in another terminal: npm run chrome" -ForegroundColor Yellow
    Write-Host "Then log in at https://chatgpt.com/" -ForegroundColor Yellow
    Write-Host ""
}

# 3. Run Python agent
if (-not $rootDir) { $rootDir = (Get-Location).Path }
$agentPath = Join-Path $rootDir "agent\agent_loop.py"
if (-not (Test-Path $agentPath)) {
    Write-Host "agent_loop.py not found: $agentPath" -ForegroundColor Red
    exit 1
}
Set-Location $rootDir
if ($Task.Count -gt 0) {
    $taskStr = $Task -join " "
    & python $agentPath $taskStr
} else {
    & python $agentPath
}

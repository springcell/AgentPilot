# Run Agent: check CDP, launch Chrome if needed, then run
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

function Test-CdpReady {
    try {
        $null = Invoke-WebRequest -Uri "$cdpUrl/json/version" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

# Check CDP, launch Chrome if needed
if (-not (Test-CdpReady)) {
    Write-Host "CDP not connected, launching Chrome..." -ForegroundColor Yellow
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
        Write-Host "CDP still not ready. Close ALL Chrome windows, then run: npm run chrome" -ForegroundColor Red
        Write-Host "Then open https://chatgpt.com/ and login, finally run: npm run run" -ForegroundColor Red
        exit 1
    }
}

Write-Host "CDP connected ($cdpUrl)" -ForegroundColor Green

# Run Agent
$agentPath = Join-Path $rootDir "src\agent.js"
if ($Message.Count -gt 0) {
    & node $agentPath @Message
} else {
    & node $agentPath
}

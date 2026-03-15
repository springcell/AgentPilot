# Check CDP connection
param([string]$url = "http://127.0.0.1:9222")

try {
    $r = Invoke-RestMethod -Uri "$url/json/version" -TimeoutSec 2
    Write-Host "CDP connected" -ForegroundColor Green
    Write-Host "  Browser: $($r.Browser)" -ForegroundColor Gray
    Write-Host "  Protocol: $($r.'Protocol-Version')" -ForegroundColor Gray
    exit 0
} catch {
    Write-Host "CDP not connected. Run: npm run chrome" -ForegroundColor Red
    exit 1
}

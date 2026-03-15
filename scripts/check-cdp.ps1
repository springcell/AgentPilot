# 检查 CDP 连接状态
param([string]$url = "http://127.0.0.1:9222")

try {
    $r = Invoke-RestMethod -Uri "$url/json/version" -TimeoutSec 2
    Write-Host "CDP 已连接" -ForegroundColor Green
    Write-Host "  Browser: $($r.Browser)" -ForegroundColor Gray
    Write-Host "  Protocol: $($r.'Protocol-Version')" -ForegroundColor Gray
    exit 0
} catch {
    Write-Host "CDP 未连接，请先运行: npm run chrome" -ForegroundColor Red
    exit 1
}

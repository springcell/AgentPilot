# Kill process on port 3000
$port = 3000
$conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
if (-not $conn) {
    Write-Host "Port $port is free" -ForegroundColor Green
    exit 0
}
$pids = $conn | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Killing: $($proc.ProcessName) (PID $procId)" -ForegroundColor Yellow
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "Port $port freed" -ForegroundColor Green

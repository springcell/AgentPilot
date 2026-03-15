# Expose local API via ngrok for Cursor
# Cursor routes requests through api2.cursor.sh, so Base URL must be publicly accessible
# Put ngrok.exe in project root or ngrok/ folder

param([int]$Port = 3000)

$rootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$localPaths = @(
    (Join-Path $rootDir "ngrok\ngrok.exe"),
    (Join-Path $rootDir "ngrok.exe")
)

$ngrokExe = $null
foreach ($p in $localPaths) {
    if (Test-Path $p) {
        $ngrokExe = $p
        break
    }
}
if (-not $ngrokExe -and (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    $ngrokExe = "ngrok"
}

if ($ngrokExe) {
    Write-Host "Starting ngrok tunnel to http://127.0.0.1:$Port" -ForegroundColor Cyan
    Write-Host "Use the HTTPS URL in Cursor Base URL (add /v1 at end)" -ForegroundColor Yellow
    & $ngrokExe http $Port
} else {
    Write-Host "ngrok not found. Put ngrok.exe in:" -ForegroundColor Red
    Write-Host "  $rootDir\ngrok\" -ForegroundColor Yellow
    Write-Host "  or $rootDir\" -ForegroundColor Yellow
    Write-Host "Download: https://ngrok.com/download" -ForegroundColor Yellow
    exit 1
}

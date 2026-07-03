# LocalCoder 起動スクリプト
# - WSL(NATモード)からWindows側Ollamaへ到達できるよう、vEthernet (WSL) の現在のIPを検出
# - OllamaがそのIPでbindされていなければ再起動してbindし直す
# - WSL内でserver.pyを起動し、Edgeでアプリウィンドウを開く
$ErrorActionPreference = "SilentlyContinue"
$distro = "Ubuntu-20.04"
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

# WSLの仮想アダプタを起こす(停止中なら起動)
$null = wsl -d $distro -- true

$ip = $null
for ($i = 0; $i -lt 10 -and -not $ip; $i++) {
    $ip = (Get-NetIPAddress -InterfaceAlias "vEthernet (WSL)" -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
    if (-not $ip) { Start-Sleep -Milliseconds 500 }
}
if (-not $ip) { $ip = "127.0.0.1" }

$desiredHost = "${ip}:11434"
$curHost = [Environment]::GetEnvironmentVariable("OLLAMA_HOST", "User")
$proc = Get-Process ollama -ErrorAction SilentlyContinue
if (-not $proc -or $curHost -ne $desiredHost) {
    [Environment]::SetEnvironmentVariable("OLLAMA_HOST", $desiredHost, "User")
    $proc | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    $env:OLLAMA_HOST = $desiredHost
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

$cmd = "pkill -f 'python3 server.py' 2>/dev/null; cd ~/localcoder && LOCALCODER_OLLAMA=http://${ip}:11434 python3 server.py"
Start-Process -WindowStyle Minimized -FilePath "wsl" -ArgumentList @("-d", $distro, "--", "bash", "-lc", $cmd)

Start-Sleep -Seconds 3

$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (Test-Path $edge) {
    Start-Process -FilePath $edge -ArgumentList "--app=http://localhost:8765/"
} else {
    Start-Process "http://localhost:8765/"
}

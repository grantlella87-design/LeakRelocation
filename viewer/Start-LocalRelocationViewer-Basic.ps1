$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas\.venv\Scripts\python.exe"
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
Set-Location $Here
Start-Process "http://127.0.0.1:8777/index.html"
& $Py -m http.server 8777 --bind 127.0.0.1

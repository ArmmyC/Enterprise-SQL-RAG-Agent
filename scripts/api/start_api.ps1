$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $Root

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
    }
}

if (-not $env:FAHMAI_API_HOST) {
    $env:FAHMAI_API_HOST = "0.0.0.0"
}
if (-not $env:FAHMAI_API_PORT) {
    $env:FAHMAI_API_PORT = "8888"
}

uvicorn api_server:app --host $env:FAHMAI_API_HOST --port ([int]$env:FAHMAI_API_PORT)

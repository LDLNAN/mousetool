$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$rawaccel = Join-Path $PSScriptRoot 'rawaccel'
if (!(Test-Path -LiteralPath $rawaccel -PathType Container)) {
    $rawaccel = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'rawaccel'
}
foreach ($name in 'Newtonsoft.Json.dll','wrapper.dll') {
    [void][Reflection.Assembly]::LoadFrom((Join-Path $rawaccel $name))
}
[DriverConfig]::GetActive().ToJSON()

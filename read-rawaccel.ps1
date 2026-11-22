$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
foreach ($name in 'Newtonsoft.Json.dll','wrapper.dll') {
    [void][Reflection.Assembly]::LoadFrom((Join-Path "$PSScriptRoot\rawaccel" $name))
}
[DriverConfig]::GetActive().ToJSON()

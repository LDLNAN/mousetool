param([string]$RawAccelDir, [switch]$Uninstall)
& (Join-Path $PSScriptRoot 'platform\windows\install.ps1') @PSBoundParameters

param([string]$RawAccelDir, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$root = Join-Path $env:LOCALAPPDATA 'mousetool'
$app = Join-Path $root 'app'
$tools = Join-Path $app 'rawaccel'
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'mousetool.lnk'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'mousetool Raw Accel.lnk'
$rootFiles = 'mousetool.py','icon.svg','install.ps1'
$legacyFiles = 'desktop_pointer.py','windows_accel.py','rawaccel_export.py','read-rawaccel.ps1','index.html'
$winFiles = 'install.ps1','read-rawaccel.ps1'
$binaries = 'writer.exe','wrapper.dll','Newtonsoft.Json.dll'
if ($Uninstall) {
    foreach ($item in @($menu,$startup,"$root\state\settings.json","$root\state\writer.lock") +
             @($rootFiles + $legacyFiles | ForEach-Object { Join-Path $app $_ }) +
             @($binaries | ForEach-Object { Join-Path $tools $_ })) {
        if (Test-Path -LiteralPath $item) { Remove-Item -LiteralPath $item }
    }
    foreach ($dir in @('mousetool','web','devices','platform')) {
        $p = Join-Path $app $dir
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    }
    foreach ($dir in @($tools, "$root\state", $app, $root)) {
        if ((Test-Path -LiteralPath $dir -PathType Container) -and !(Get-ChildItem -LiteralPath $dir -Force | Select-Object -First 1)) {
            Remove-Item -LiteralPath $dir
        }
    }
    Write-Output 'Removed launcher, logon entry, saved curve and application files.'
    return
}
$python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$python) { throw 'Install Python 3.10 or newer for this user first.' }
$exe = & $python.Source -c "import sys; assert sys.version_info >= (3,10); print(sys.executable)"
if ($LASTEXITCODE -ne 0 -or !$exe) { throw 'Python 3.10 or newer is required.' }
if (!$RawAccelDir) { $RawAccelDir = "$env:USERPROFILE\Downloads\RawAccel_v1.7.1\RawAccel" }
foreach ($name in $binaries) {
    if (!(Test-Path -LiteralPath (Join-Path $RawAccelDir $name) -PathType Leaf)) { throw "Missing $name in $RawAccelDir" }
}
New-Item -ItemType Directory -Path $tools -Force | Out-Null
if ($repo -ne $app) {
    foreach ($name in $rootFiles) {
        $src = Join-Path $repo $name
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            Copy-Item -LiteralPath $src -Destination $app -Force
        }
    }
    foreach ($dir in @('mousetool','web','devices')) {
        $dst = Join-Path $app $dir
        if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
        Copy-Item -LiteralPath (Join-Path $repo $dir) -Destination $dst -Recurse -Force
        Get-ChildItem -LiteralPath $dst -Directory -Recurse -Filter '__pycache__' | Remove-Item -Recurse -Force
    }
    $winDst = Join-Path $app 'platform\windows'
    New-Item -ItemType Directory -Path $winDst -Force | Out-Null
    foreach ($name in $winFiles) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $winDst -Force
    }
}
foreach ($name in $binaries) {
    Copy-Item -LiteralPath (Join-Path $RawAccelDir $name) -Destination $tools -Force
    Unblock-File -LiteralPath (Join-Path $tools $name)
}
$shell = New-Object -ComObject WScript.Shell
function Shortcut($path,$target,$arguments) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $app
    $link.Save()
}
Shortcut $menu $exe ('"' + "$app\mousetool.py" + '" serve')
Shortcut $startup (Join-Path (Split-Path $exe) 'pythonw.exe') '-m mousetool.accel.windows'
Write-Output 'Installed for this user. Open mousetool from Start.'

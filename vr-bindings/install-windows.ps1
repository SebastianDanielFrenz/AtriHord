# Installs the "media-dl d-pad" SteamVR binding for the media-dl VR viewer in Chrome with Steam Frame
# controllers (see README.md next to this file). Run it on the Windows PC that runs SteamVR:
#
#   irm https://raw.githubusercontent.com/SebastianDanielFrenz/AtriHord/main/vr-bindings/install-windows.ps1 | iex
#
# It downloads the binding from the repository, points it at the installed Chrome version and writes it to
# Documents\steamvr\input\imports, where SteamVR picks it up. Running it again updates the binding.
# -Source takes another URL or a local file; -ChromeMajor overrides the detected Chrome version.
param(
    [string]$Source = "https://raw.githubusercontent.com/SebastianDanielFrenz/AtriHord/main/vr-bindings/chrome_frame_controller_dpad.json",
    [int]$ChromeMajor = 0
)
$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath $Source) {
    $text = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
} else {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $text = (Invoke-WebRequest -Uri $Source -UseBasicParsing).Content
}
$binding = $text | ConvertFrom-Json
if ($binding.name -ne "media-dl d-pad" -or -not $binding.bindings) { throw "The downloaded file is not the media-dl d-pad binding: $Source" }

# SteamVR names Chrome's entry after its major version ("google chrome 156.chrome.exe").
if ($ChromeMajor -le 0) {
    $version = $null
    foreach ($key in "HKCU:\Software\Google\Chrome\BLBeacon", "HKLM:\Software\Google\Chrome\BLBeacon") {
        $p = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($p -and $p.version) { $version = $p.version; break }
    }
    if (-not $version) {
        foreach ($exe in "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") {
            if (Test-Path -LiteralPath $exe) { $version = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion; break }
        }
    }
    if (-not $version) { throw "Chrome not found. Install Chrome, or pass -ChromeMajor <version>." }
    $ChromeMajor = [int]($version.Split(".")[0])
}
$appKey = "system.generated.openxr.google chrome $ChromeMajor.chrome.exe"
# Swap only the app_key line, so the rest of the file stays exactly as in the repository.
$text = [regex]::Replace($text, '("app_key"\s*:\s*)"[^"]*"', ('$1"' + $appKey + '"'))

$docs = [Environment]::GetFolderPath("MyDocuments")   # follows a Documents folder moved to OneDrive
$dir = Join-Path $docs "steamvr\input\imports"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$target = Join-Path $dir "chrome_frame_controller_dpad.json"
[IO.File]::WriteAllText($target, $text, (New-Object Text.UTF8Encoding $false))

Write-Host "Installed the media-dl d-pad binding for Chrome $ChromeMajor at $target"
Write-Host "Restart SteamVR (or start it) and open the VR page in Chrome. If the buttons still act the old way:"
Write-Host "SteamVR > Settings > Controllers > Manage Controller Bindings > Chrome > media-dl d-pad"

param([string]$ProgramDirectory = $(if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "FaceMaskStudio.exe")) { $PSScriptRoot } else { Split-Path -Parent $PSScriptRoot }))

# One-time, explicit resource preparation. The desktop app never runs this script.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$packageUrl = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"
$packageHash = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
$binaryHash = "3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec"
$programRoot = (Resolve-Path -LiteralPath $ProgramDirectory).Path
if (-not (Test-Path -LiteralPath (Join-Path $programRoot "FaceMaskStudio.exe"))) {
    throw "Select the extracted program folder containing FaceMaskStudio.exe."
}
$assetsRoot = Join-Path $programRoot "_internal\assets"
if (-not (Test-Path -LiteralPath $assetsRoot -PathType Container)) {
    throw "The _internal/assets directory is missing. Extract the entire program ZIP."
}
$destination = Join-Path $assetsRoot "ffmpeg.exe"
if (Test-Path -LiteralPath $destination) {
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $binaryHash) {
        Write-Host "Video component already verified. You can use the app offline."
        exit 0
    }
    throw "An existing ffmpeg.exe has a different hash. It was not overwritten."
}

$downloadRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("FaceMaskStudio-video-" + [guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $downloadRoot "ffmpeg.zip"
$pendingPath = Join-Path $downloadRoot "ffmpeg.exe"
New-Item -ItemType Directory -Path $downloadRoot | Out-Null
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Host "Downloading the video component from the official Gyan FFmpeg build site..."
    Write-Host $packageUrl
    Invoke-WebRequest -Uri $packageUrl -OutFile $archivePath -UseBasicParsing
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $packageHash) {
        throw "Downloaded ZIP checksum does not match. Nothing was installed."
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entries = @($archive.Entries | Where-Object { $_.FullName -eq "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe" })
        if ($entries.Count -ne 1) { throw "Unexpected ZIP layout. Nothing was installed." }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0], $pendingPath, $false)
    }
    finally { $archive.Dispose() }
    if ((Get-FileHash -LiteralPath $pendingPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $binaryHash) {
        throw "Extracted video component checksum does not match. Nothing was installed."
    }
    Copy-Item -LiteralPath $pendingPath -Destination $destination -ErrorAction Stop
    Write-Host "Video component verified and installed. All app processing now works offline."
}
finally {
    # This exact directory was created above, under the resolved system temp folder.
    $resolvedDownload = [System.IO.Path]::GetFullPath($downloadRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolvedDownload.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
        [System.IO.Path]::GetFileName($resolvedDownload).StartsWith("FaceMaskStudio-video-")) {
        Remove-Item -LiteralPath $resolvedDownload -Recurse -Force
    }
}

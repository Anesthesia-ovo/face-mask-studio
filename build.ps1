param(
    [string]$Python = "python",
    [switch]$SkipInstall,
    [switch]$SkipDownload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE : $Program"
    }
}

if (Test-Path -LiteralPath $venvPython) {
    $Python = $venvPython
}
$versionOutput = & $Python -c "import json,struct,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'bits':struct.calcsize('P')*8,'platform':sys.platform}))"
if ($LASTEXITCODE -ne 0) { throw "Cannot run the selected Python interpreter." }
$version = ($versionOutput | Out-String) | ConvertFrom-Json
if ($version.major -ne 3 -or $version.minor -ne 12 -or $version.bits -ne 64 -or $version.platform -ne "win32") {
    throw "Build requires Windows x64 Python 3.12. Use -Python with a Python 3.12 executable path."
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked -Program $Python -Arguments @("-m", "venv", (Join-Path $repoRoot ".venv"))
}

Push-Location -LiteralPath $repoRoot
try {
    if (-not $SkipInstall) {
        Invoke-Checked -Program $venvPython -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-build.txt")
    }
    $resourceArguments = @("scripts\fetch_resources.py")
    if ($SkipDownload) { $resourceArguments += "--verify-only" }
    Invoke-Checked -Program $venvPython -Arguments $resourceArguments
    Invoke-Checked -Program $venvPython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-p", "test_engine.py", "-v")

    $arguments = @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", "FaceMaskStudio", "--icon", (Join-Path $repoRoot "assets\app.ico"),
        "--distpath", (Join-Path $repoRoot "dist"), "--workpath", (Join-Path $repoRoot ".build\work"),
        "--specpath", (Join-Path $repoRoot ".build"), "--paths", (Join-Path $repoRoot "src"),
        "--add-data", ((Join-Path $repoRoot "models") + ";models"),
        "--add-data", ((Join-Path $repoRoot "assets") + ";assets"),
        "--add-data", ((Join-Path $repoRoot "licenses") + ";LICENSES"),
        "--add-data", ((Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") + ";."),
        "--add-data", ((Join-Path $repoRoot "LICENSE") + ";."),
        (Join-Path $repoRoot "src\launcher.py")
    )
    Invoke-Checked -Program $venvPython -Arguments $arguments
    Write-Host "Build complete: dist\FaceMaskStudio\FaceMaskStudio.exe"
    Write-Host "Distribute the entire dist\FaceMaskStudio folder, including its _internal folder."
}
finally {
    Pop-Location
}

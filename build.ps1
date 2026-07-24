param(
    [ValidateSet("OneDir", "OneFile", "All")]
    [string]$Mode = "OneDir",
    [string]$OutputRoot = "",
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $MyInvocation.MyCommand.Path)
)
$ProjectParent = Split-Path -Parent $ProjectRoot

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = $ProjectRoot
}
else {
    $OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
}
if ([string]::IsNullOrWhiteSpace($VenvPath)) {
    $VenvPath = Join-Path $ProjectParent ".LLMBatDesk-build-venv"
}
$VenvPath = [System.IO.Path]::GetFullPath($VenvPath)

function Test-PathWithin {
    param([string]$Child, [string]$Parent)
    $ParentWithSeparator = $Parent.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    return $Child.StartsWith(
        $ParentWithSeparator, [System.StringComparison]::OrdinalIgnoreCase
    )
}

if ($VenvPath -eq $ProjectRoot -or (Test-PathWithin $VenvPath $ProjectRoot)) {
    throw "Build virtual environment must be outside the source folder: $VenvPath"
}

$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$ResourceCompiler = Join-Path $VenvPath "Scripts\pyside6-rcc.exe"
$BuildRoot = Join-Path $OutputRoot "build"
$DistRoot = Join-Path $OutputRoot "dist"

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Clear-GeneratedPath {
    param([string]$Path)
    $ResolvedCandidate = [System.IO.Path]::GetFullPath($Path)
    if (
        $ResolvedCandidate -eq $OutputRoot -or
        -not (Test-PathWithin $ResolvedCandidate $OutputRoot)
    ) {
        throw "Refusing to clear a path outside the output root: $ResolvedCandidate"
    }
    if (Test-Path -LiteralPath $ResolvedCandidate) {
        Remove-Item -LiteralPath $ResolvedCandidate -Recurse -Force
    }
}

function Test-Package {
    param([string]$Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Executable was not created: $Executable"
    }
    $SmokeData = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) ("LLMBatDesk-smoke-" + [guid]::NewGuid().ToString("N"))
    $PreviousDataDirectory = $env:LLMBATDESK_DATA_DIR
    try {
        $env:LLMBATDESK_DATA_DIR = $SmokeData
        $SmokeProcess = Start-Process -FilePath $Executable -ArgumentList "--smoke-test" `
            -Wait -PassThru -WindowStyle Hidden
        if ($SmokeProcess.ExitCode -ne 0) {
            throw "Packaged GUI smoke test failed with exit code $($SmokeProcess.ExitCode)"
        }
    }
    finally {
        $env:LLMBATDESK_DATA_DIR = $PreviousDataDirectory
        if (Test-Path -LiteralPath $SmokeData) {
            Remove-Item -LiteralPath $SmokeData -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create build virtual environment: $VenvPath"
    }
}

Invoke-CheckedProcess $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check",
    "-r", (Join-Path $ProjectRoot "requirements.txt"),
    "pyinstaller>=6.10,<7"
)

Push-Location $ProjectRoot
try {
    Invoke-CheckedProcess $ResourceCompiler @(
        "src\llmbatdesk\qt\resources.qrc",
        "-o", "src\llmbatdesk\qt\resources_rc.py"
    )

    if ($Mode -in @("OneDir", "All")) {
        $OneDirWork = Join-Path $BuildRoot "OneDir"
        $OneDirOutput = Join-Path $DistRoot "LLMBatDesk"
        Clear-GeneratedPath $OneDirWork
        Clear-GeneratedPath $OneDirOutput
        Invoke-CheckedProcess $VenvPython @(
            "-m", "PyInstaller", "--noconfirm", "--clean",
            "--workpath", $OneDirWork,
            "--distpath", $DistRoot,
            "LLMBatDesk.spec"
        )
        $OneDirExecutable = Join-Path $OneDirOutput "LLMBatDesk.exe"
        Test-Package $OneDirExecutable
        Write-Host "One-directory build complete: $OneDirExecutable"
    }

    if ($Mode -in @("OneFile", "All")) {
        $OneFileWork = Join-Path $BuildRoot "OneFile"
        $PortableRoot = Join-Path $DistRoot "portable"
        $OneFileExecutable = Join-Path $PortableRoot "LLMBatDesk-Portable.exe"
        Clear-GeneratedPath $OneFileWork
        Clear-GeneratedPath $OneFileExecutable
        Invoke-CheckedProcess $VenvPython @(
            "-m", "PyInstaller", "--noconfirm", "--clean",
            "--workpath", $OneFileWork,
            "--distpath", $PortableRoot,
            "LLMBatDesk-OneFile.spec"
        )
        Test-Package $OneFileExecutable
        Write-Host "One-file build complete: $OneFileExecutable"
    }
}
finally {
    Pop-Location
}

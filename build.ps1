param(
    [ValidateSet("OneDir", "OneFile", "All")]
    [string]$Mode = "OneDir"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ResourceCompiler = Join-Path $ProjectRoot ".venv\Scripts\pyside6-rcc.exe"

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Test-Package {
    param([string]$Executable, [string]$DataName)
    if (-not (Test-Path -LiteralPath $Executable)) {
        throw "Executable was not created: $Executable"
    }
    $PreviousDataDirectory = $env:LLMBATDESK_DATA_DIR
    try {
        $env:LLMBATDESK_DATA_DIR = Join-Path $ProjectRoot "build\$DataName"
        $SmokeProcess = Start-Process -FilePath $Executable -ArgumentList "--smoke-test" `
            -Wait -PassThru -WindowStyle Hidden
        if ($SmokeProcess.ExitCode -ne 0) {
            throw "Packaged GUI smoke test failed with exit code $($SmokeProcess.ExitCode): $Executable"
        }
        $env:LLMBATDESK_DATA_DIR = Join-Path $ProjectRoot "build\$DataName-model-library"
        $ExtensionSmoke = Start-Process -FilePath $Executable `
            -ArgumentList "--smoke-test-model-library" -Wait -PassThru -WindowStyle Hidden
        if ($ExtensionSmoke.ExitCode -ne 0) {
            throw "Packaged model-library smoke test failed with exit code $($ExtensionSmoke.ExitCode): $Executable"
        }
    }
    finally {
        $env:LLMBATDESK_DATA_DIR = $PreviousDataDirectory
    }
    Invoke-CheckedProcess $VenvPython @(
        (Join-Path $ProjectRoot "scripts\verify_windows_exe.py"),
        $Executable
    )
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}

Invoke-CheckedProcess $VenvPython @(
    "-m", "pip", "install", "--disable-pip-version-check",
    "-r", (Join-Path $ProjectRoot "requirements-dev.txt")
)

Push-Location $ProjectRoot
try {
    Invoke-CheckedProcess $ResourceCompiler @(
        "src\llmbatdesk\qt\resources.qrc",
        "-o", "src\llmbatdesk\qt\resources_rc.py"
    )
    Invoke-CheckedProcess $VenvPython @("-m", "pytest")

    if ($Mode -in @("OneDir", "All")) {
        Invoke-CheckedProcess $VenvPython @(
            "-m", "PyInstaller", "--noconfirm", "--clean", "LLMBatDesk.spec"
        )
        $OneDirExecutable = Join-Path $ProjectRoot "dist\LLMBatDesk\LLMBatDesk.exe"
        Test-Package $OneDirExecutable "smoke-onedir"
        Write-Host "One-directory build complete: $OneDirExecutable"
    }

    if ($Mode -in @("OneFile", "All")) {
        Invoke-CheckedProcess $VenvPython @(
            "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", "dist\portable", "LLMBatDesk-OneFile.spec"
        )
        $OneFileExecutable = Join-Path $ProjectRoot "dist\portable\LLMBatDesk-Portable.exe"
        Test-Package $OneFileExecutable "smoke-onefile"
        Write-Host "One-file build complete: $OneFileExecutable"
    }

    if ($Mode -eq "All") {
        $InnoCandidates = @(
            (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
        $InnoCompiler = $InnoCandidates | Select-Object -First 1
        if ($InnoCompiler) {
            Invoke-CheckedProcess $InnoCompiler @("installer\LLMBatDesk.iss")
            $Installer = Join-Path $ProjectRoot "dist\installer\LLMBatDesk-Setup.exe"
            if (-not (Test-Path -LiteralPath $Installer)) {
                throw "Inno Setup completed but installer was not found: $Installer"
            }
            Invoke-CheckedProcess $VenvPython @(
                (Join-Path $ProjectRoot "scripts\verify_windows_exe.py"), $Installer
            )
            Write-Host "Installer build complete: $Installer"
        }
        else {
            Write-Warning "Inno Setup is unavailable. The validated installer script remains at installer\LLMBatDesk.iss; no system software was installed."
        }
    }
}
finally {
    Pop-Location
}

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements-dev.txt")
Push-Location $ProjectRoot
try {
    & $VenvPython -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Tests failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}


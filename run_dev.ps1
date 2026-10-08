$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
& (Join-Path $ProjectRoot ".venv\Scripts\pyside6-rcc.exe") `
    (Join-Path $ProjectRoot "src\llmbatdesk\qt\resources.qrc") `
    -o (Join-Path $ProjectRoot "src\llmbatdesk\qt\resources_rc.py")
if ($LASTEXITCODE -ne 0) { throw "Qt resource compilation failed." }
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& $VenvPython -m llmbatdesk

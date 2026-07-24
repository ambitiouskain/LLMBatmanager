# LLMBatmanager

[简体中文](README.md)

**LLMBatmanager** (application name: **LLMBatDesk**) is a Windows desktop manager for local model launch scripts. It organizes, parses, starts, and stops `llama.cpp`, Ollama, and other BAT/CMD configurations.

It does not proxy inference requests, so it does not interfere with or limit model generation speed.

## Features

- Scan, organize, and favorite `.bat` / `.cmd` launch scripts
- Parse backends, model paths, ports, and common launch parameters
- Start, stop, and restart local model services
- Detect port conflicts and launch through a temporary port override
- Check llama.cpp/OpenAI-compatible and Ollama API readiness
- View, search, filter, and save runtime logs
- Run scripts in background or visible-console mode
- Trust confirmation based on script content hash and working directory
- Manage logs, launch history, and temporary files
- Dark/light themes and Per-Monitor-V2 DPI support

## Supported Backends

Dedicated parsing and API checks are currently provided for:

- `llama.cpp`
- Ollama

Other BAT/CMD scripts use the generic parser. Parameter detection and API status checks may be limited.

## Run from Source

Requirements: Windows and Python 3.11 or later.

```powershell
git clone https://github.com/ambitiouskain/LLMBatmanager.git
cd LLMBatmanager

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

python -m llmbatdesk
```

## Build

Build both OneDir and OneFile packages:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode All
```

Build either package separately:

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode OneDir
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode OneFile
```

Typical output:

```text
dist/LLMBatDesk/LLMBatDesk.exe
dist/portable/LLMBatDesk-Portable.exe
```

## User Data

Settings, launch history, logs, and temporary port-override scripts are stored under:

```text
%LOCALAPPDATA%\LLMBatDesk
```

Deleting the portable EXE does not automatically remove this directory. Data can be reviewed or cleaned from **Settings → Storage & Cleanup**.

## Security

BAT/CMD files can execute arbitrary system commands. Successful parsing does not mean a script is safe.

- Run only scripts from trusted sources whose contents you understand
- Review the executable, working directory, model path, and port before launch
- Temporary port overrides modify only a generated copy under LocalAppData, never the original script
- Do not store API keys, passwords, or other secrets in launch scripts

## Current Version

`1.2.3`

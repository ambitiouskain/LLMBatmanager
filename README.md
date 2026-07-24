# LLMBatmanager

[English](README_EN.md)

**LLMBatmanager**（程序名称：**LLMBatDesk**）是一款 Windows 本地模型启动脚本管理器，用于集中管理、解析、启动和停止 `llama.cpp`、Ollama 及其他 BAT/CMD 启动配置。

它不代理模型推理请求，因此不会介入或限制模型的生成速度。

## 功能

- 扫描、分类和收藏 `.bat` / `.cmd` 启动脚本
- 解析后端、模型路径、端口及常用启动参数
- 启动、停止和重启本地模型服务
- 检测端口冲突，并使用临时脚本切换端口
- 检查 llama.cpp/OpenAI 兼容 API 与 Ollama API 状态
- 查看、搜索、筛选和保存运行日志
- 支持后台启动和可见控制台模式
- 基于脚本内容哈希与工作目录的信任确认
- 管理日志、历史记录和临时文件
- 支持深色/浅色主题与 Per-Monitor-V2 DPI

## 支持范围

目前对以下后端提供专门解析和 API 检查：

- `llama.cpp`
- Ollama

其他 BAT/CMD 脚本将使用通用解析流程，部分参数识别和 API 状态检查可能受限。

## 从源码运行

要求：Windows、Python 3.11 或更高版本。

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

## 构建

构建 OneDir 和 OneFile：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode All
```

单独构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode OneDir
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Mode OneFile
```

典型输出：

```text
dist/LLMBatDesk/LLMBatDesk.exe
dist/portable/LLMBatDesk-Portable.exe
```

## 用户数据

配置、运行历史、日志和临时端口脚本默认保存在：

```text
%LOCALAPPDATA%\LLMBatDesk
```

删除便携版 EXE 不会自动删除该目录。可以通过“设置 → 存储与清理”查看或清理数据。

## 安全说明

BAT/CMD 可以执行任意系统命令。解析成功不代表脚本安全。

- 只运行来源可信且内容明确的脚本
- 启动前检查可执行文件、工作目录、模型路径和端口
- 临时端口功能仅修改 LocalAppData 中生成的临时副本，不修改原始脚本
- 不要在脚本中保存 API 密钥、密码或其他敏感信息

## 当前版本

`1.2.3`

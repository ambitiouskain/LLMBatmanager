@echo off
rem Supported example only; edit paths before use. LLMBatDesk never runs examples automatically.
"D:\llama\llama-server.exe" ^
  --model "D:\llama\models\example.gguf" ^
  --host 127.0.0.1 ^
  --port 8080 ^
  --ctx-size 32768 ^
  --n-gpu-layers 99 ^
  --flash-attn on ^
  --temp 0.85 ^
  --top-p 0.95


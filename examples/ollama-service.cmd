@echo off
rem Service-only example: no fixed model is implied.
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NUM_PARALLEL=2"
ollama serve


from __future__ import annotations

from pathlib import Path

import pytest

from llmbatdesk.domain.models import Backend, ParseConfidence
from llmbatdesk.parsing import parse_script_bytes


def parse(text: str, encoding: str = "utf-8"):
    return parse_script_bytes(text.encode(encoding), Path("测试 脚本.bat"))


def test_one_line_llama() -> None:
    result = parse("llama-server.exe -m model.gguf --port 8080 -c 4096 -ngl 20")
    assert result.backend == Backend.LLAMA_CPP
    assert (result.model_name, result.configured_port, result.context_size, result.gpu_layers) == (
        "model.gguf", 8080, 4096, 20
    )
    assert result.confidence == ParseConfidence.FULL


def test_multiline_and_chinese_quoted_paths(llama_text: str) -> None:
    result = parse(llama_text)
    assert result.executable == r"C:\Program Files\llama\llama-server.exe"
    assert result.model_path == r"C:\模型\Gemma 4\model.gguf"
    assert result.parameters["flash_attention"] is True
    assert result.parameters["temperature"] == 0.85
    assert result.parameters["top_p"] == 0.95


@pytest.mark.parametrize("fragment", ["--port 8080", "--port=8080"])
def test_port_forms(fragment: str) -> None:
    result = parse(f"llama-server.exe -m m.gguf {fragment}")
    assert result.configured_port == 8080
    assert result.port_source is not None


@pytest.mark.parametrize("assignment", ["set PORT=8080", 'set "PORT=8080"'])
def test_port_variable_forms(assignment: str) -> None:
    result = parse(f"{assignment}\r\nllama-server.exe -m m.gguf --port %PORT%")
    assert result.configured_port == 8080
    assert result.port_source and result.port_source.style == "environment"


def test_static_variable_expansion() -> None:
    result = parse(
        'set "BASE=C:\\模型"\nset "MODEL=%BASE%\\x.gguf"\n'
        'llama-server.exe --model "%MODEL%" --port 8000'
    )
    assert result.model_path == r"C:\模型\x.gguf"


def test_ollama_serve_is_valid_service() -> None:
    result = parse("ollama serve")
    assert result.backend == Backend.OLLAMA
    assert result.model_name is None
    assert result.configured_port == 11434
    assert "未指定固定模型" in result.warnings[0]


def test_ollama_run_model_and_environment() -> None:
    result = parse('set "OLLAMA_HOST=0.0.0.0:12434"\nollama run qwen2.5:14b')
    assert result.backend == Backend.OLLAMA
    assert result.model_name == "qwen2.5:14b"
    assert result.configured_port == 12434
    assert result.client_address == "http://127.0.0.1:12434"


def test_unknown_backend_is_generic_and_launchable() -> None:
    result = parse('"C:\\Tools\\unknown server.exe" --port 9000')
    assert result.backend == Backend.GENERIC
    assert result.executable == r"C:\Tools\unknown server.exe"
    assert result.confidence == ParseConfidence.PARTIAL


def test_unrelated_server_exe_not_mislabeled() -> None:
    result = parse("server.exe --listen 8080")
    assert result.backend == Backend.GENERIC


@pytest.mark.parametrize(
    ("line", "attribute"),
    [("call other.cmd", "uses_call"), ('start "" llama-server.exe -m x.gguf', "uses_start")],
)
def test_call_and_start_detection(line: str, attribute: str) -> None:
    result = parse(line)
    assert getattr(result, attribute)
    assert result.confidence != ParseConfidence.FULL


@pytest.mark.parametrize("dynamic", ["goto branch\n:branch", "set /p PORT=端口：", "echo !PORT!"])
def test_dynamic_syntax_refuses_to_claim_full(dynamic: str) -> None:
    result = parse(dynamic + "\nllama-server.exe -m x.gguf --port 8080")
    assert result.confidence == ParseConfidence.PARTIAL
    assert result.dynamic_reasons
    assert result.port_source is None


def test_malformed_quoting_partial() -> None:
    result = parse('"llama-server.exe --model broken.gguf --port 8080')
    assert result.dynamic_reasons
    assert result.confidence != ParseConfidence.FULL


def test_missing_model_is_reported(tmp_path: Path) -> None:
    missing = tmp_path / "missing.gguf"
    result = parse(f'llama-server.exe --model "{missing}" --port 8080')
    assert result.model_exists is False


def test_unknown_options_preserved() -> None:
    result = parse("llama-server.exe -m x.gguf --port 8080 --future-option abc")
    assert "--future-option" in result.other_arguments
    assert "abc" in result.other_arguments


def test_api_key_hidden_from_evidence_and_other_arguments() -> None:
    result = parse("llama-server.exe -m x.gguf --port 8080 --api-key super-secret")
    assert result.has_api_key
    combined = repr(result.evidence) + repr(result.other_arguments)
    assert "super-secret" not in combined


@pytest.mark.parametrize(("newline", "expected"), [("\r\n", "\r\n"), ("\n", "\n")])
def test_line_endings(newline: str, expected: str) -> None:
    result = parse(f"@echo off{newline}ollama serve{newline}")
    assert result.newline == expected


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030", "cp932"])
def test_common_encodings(encoding: str) -> None:
    result = parse("rem 中文\nollama serve", encoding)
    assert result.backend == Backend.OLLAMA


def test_bind_and_client_addresses_distinct() -> None:
    result = parse("llama-server.exe -m x.gguf --host 0.0.0.0 --port 8080")
    assert result.bind_address == "http://0.0.0.0:8080/v1"
    assert result.client_address == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("-ngl 99", 99),
        ("-ngl=99", 99),
        ("-ngl auto", "auto"),
        ("-ngl=auto", "auto"),
        ("-ngl all", "all"),
        ("-ngl=all", "all"),
        ("--gpu-layers all", "all"),
        ("--gpu-layers=all", "all"),
        ("--n-gpu-layers all", "all"),
        ("--n-gpu-layers=all", "all"),
    ],
)
def test_gpu_layers_accepts_integer_and_symbolic_values(
    argument: str, expected: int | str
) -> None:
    result = parse(f"llama-server.exe -m x.gguf --port 8080 {argument}")
    assert result.gpu_layers == expected
    assert result.confidence == ParseConfidence.FULL
    assert not any("gpu" in reason.casefold() or "ngl" in reason.casefold()
                   for reason in result.dynamic_reasons)
    evidence = next(item for item in result.evidence if item.field == "gpu_layers")
    assert evidence.normalized_token == argument

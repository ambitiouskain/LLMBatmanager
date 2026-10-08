from __future__ import annotations

from pathlib import Path

import pytest

from llmbatdesk.domain.models import PortOccupant
from llmbatdesk.parsing import parse_script_bytes
from llmbatdesk.parsing.parser import runtime_api_address
from llmbatdesk.runtime.override import OverrideRefused, generate_port_override
from llmbatdesk.runtime.ports import ConflictDecision, conflict_choices


@pytest.mark.parametrize(
    "text",
    [
        "llama-server.exe -m x.gguf --port 8080\r\nrem keep 8080 here\r\n",
        "llama-server.exe -m x.gguf --port=8080\nrem keep 8080 here\n",
        "set PORT=8080\r\nllama-server.exe -m x.gguf --port %PORT%\r\n",
        'set "PORT=8080"\nllama-server.exe -m x.gguf --port %PORT%\n',
    ],
)
def test_temporary_override_exact_and_original_unchanged(tmp_path: Path, text: str) -> None:
    original = text.encode()
    script = tmp_path / "x.bat"
    script.write_bytes(original)
    parsed = parse_script_bytes(original, script)
    override = generate_port_override(parsed, original, 8081, tmp_path / "temporary")
    assert script.read_bytes() == original
    assert override.data.count(b"8081") == 1
    assert override.data.count(b"keep 8080") == original.count(b"keep 8080")
    assert "-8080-" not in override.path.name
    assert "-8081-" in override.path.name
    assert "8080" in override.diff and "8081" in override.diff


@pytest.mark.parametrize(
    "text",
    [
        "set /p PORT=port:\nllama-server.exe -m x.gguf --port %PORT%",
        "goto x\n:x\nllama-server.exe -m x.gguf --port 8080",
        "call port.cmd\nllama-server.exe -m x.gguf --port 8080",
    ],
)
def test_dynamic_port_override_refusal(tmp_path: Path, text: str) -> None:
    data = text.encode()
    parsed = parse_script_bytes(data, tmp_path / "x.bat")
    with pytest.raises(OverrideRefused):
        generate_port_override(parsed, data, 8081, tmp_path / "out")


def test_runtime_address_uses_actual_port() -> None:
    parsed = parse_script_bytes(
        b"llama-server.exe -m x.gguf --host 0.0.0.0 --port 8080", Path("x.bat")
    )
    assert runtime_api_address(parsed, 8088) == "http://127.0.0.1:8088/v1"


def test_managed_conflict_has_exactly_four_primary_choices() -> None:
    occupant = PortOccupant(port=8080, pid=1, managed_launch_id="managed")
    assert conflict_choices(occupant) == (
        ConflictDecision.STOP_AND_START, ConflictDecision.STOP_ONLY,
        ConflictDecision.ALTERNATE_PORT, ConflictDecision.CANCEL,
    )


def test_unmanaged_conflict_never_offers_stop() -> None:
    choices = conflict_choices(PortOccupant(port=8080, pid=2))
    assert ConflictDecision.STOP_ONLY not in choices
    assert ConflictDecision.STOP_AND_START not in choices
    assert choices == (ConflictDecision.ALTERNATE_PORT, ConflictDecision.CANCEL, "process_info")


def test_automatic_free_port_selection() -> None:
    from llmbatdesk.runtime.ports import PsutilPortInspector
    port = PsutilPortInspector().find_free(49152)
    assert 49152 <= port <= 65535

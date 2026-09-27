"""Compatibility import surface for the packaged AgentBus service."""
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "src" / "agentbus_service.py"
exec(compile(_SOURCE.read_bytes(), str(_SOURCE), "exec"), globals())

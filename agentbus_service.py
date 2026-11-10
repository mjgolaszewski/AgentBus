"""Compatibility import surface for the packaged AgentBus service."""
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "src" / "agentbus_service.py"
exec(compile(_SOURCE.read_bytes(), str(_SOURCE), "exec"), globals())


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run("agentbus_service:create_app", factory=True,
                host=os.environ.get("AGENTBUS_HOST", "127.0.0.1"),
                port=int(os.environ.get("AGENTBUS_PORT", "8766")), access_log=False)

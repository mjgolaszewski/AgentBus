"""Read-only service health and authenticated transport status."""

from __future__ import annotations

from fastapi import Request


def api_health(request: Request) -> dict:
    return {"status": "ok"}


def api_status(request: Request) -> dict:
    return {"status": "ok", "slack_connected": request.app.state.socket.is_connected(),
            "database_bytes": request.app.state.settings.database_bytes()}

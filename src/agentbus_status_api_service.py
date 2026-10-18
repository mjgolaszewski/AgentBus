"""Read-only service health and authenticated transport status."""

from __future__ import annotations

import hmac

from fastapi import Request


def api_health(request: Request) -> dict:
    return {"status": "ok"}


def _operator(request):
    token=request.headers.get('Authorization','')
    scheme,_,value=token.partition(' ')
    expected=request.app.state.settings.operator_token
    return bool(expected and scheme.lower()=='bearer' and hmac.compare_digest(value.encode(),expected.encode()))


def authenticate_status(request:Request):
    if not _operator(request):
        from src.agentbus_service import authenticate
        authenticate(request,request.headers.get('Authorization'))


def api_status(request: Request) -> dict:
    result={"status":"ok","slack_connected":bool(request.app.state.socket is not None and request.app.state.socket.is_connected())}
    if _operator(request):
        result.update(operator_dashboard(request.app))
    return result


def service_dashboard(settings, store, *, socket=None, bridge=None, version=None, started_at=None):
    """Privileged read model; never probe providers, expose credentials or infer remote health."""
    import os
    import platform
    import time
    import tomllib
    from pathlib import Path

    if version is None:
        version = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())['project']['version']
    connected = None
    observe = getattr(socket, 'is_connected', None)
    if callable(observe):
        try:
            connected = bool(observe())
        except (OSError, RuntimeError):
            connected = None
    channels = []
    with store._lock:
        retained_channel_count = store._db.execute('SELECT count(*) FROM bus_channels').fetchone()[0]
        placeholders = ','.join('?' for _ in settings.channels)
        rows = store._db.execute(
            f'SELECT channel,inbox_id FROM bus_channels ORDER BY channel IN ({placeholders}) DESC,channel LIMIT 64',
            settings.channels).fetchall()
        for row in rows:
            channel = row['channel']
            states = dict(store._db.execute(
                'SELECT state,count(*) FROM local_delivery_obligations WHERE channel=? GROUP BY state',
                (channel,)).fetchall())
            sessions = dict(store._db.execute(
                'SELECT s.state,count(*) FROM participation_sessions s JOIN chat_channels c USING(chat_id) '
                'WHERE c.channel=? GROUP BY s.state', (channel,)).fetchall())
            channels.append({'channel':channel, 'enabled':channel in settings.channels, 'inbox_id':row['inbox_id'],
                             'messages':store._db.execute('SELECT count(*) FROM messages WHERE channel=?',(channel,)).fetchone()[0],
                             'high_water_cursor':store._db.execute('SELECT COALESCE(max(position),0) FROM channel_positions WHERE channel=?',(channel,)).fetchone()[0],
                             'sessions':sessions,'delivery':states})
        custody = dict(store._db.execute('SELECT state,count(*) FROM bridge_obligations GROUP BY state').fetchall())
        checkpoints = store._db.execute('SELECT count(*) FROM federation_checkpoints').fetchone()[0]
    return {'version':version, 'runtime':{'python':platform.python_version(),
            'implementation':platform.python_implementation(),'platform':platform.system(),'pid':os.getpid(),
            'uptime_seconds':max(0, int(time.monotonic()-started_at)) if started_at is not None else None},
            'slack_connected':connected, 'channels':channels,'retained_channel_count':retained_channel_count,
            'channel_details_truncated':len(channels)<retained_channel_count,'database_bytes':settings.database_bytes(),
            'delivery_mode':'local_custody_v1' if settings.local_delivery_version==1 else 'immediate',
            'local_delivery_capacity':store.local_delivery.capacity(),
            'protocol_version':2,'participation_version':2,
            'federation':{'configured':settings.federation_config is not None,'initialized':bridge is not None,
                          'channel':getattr(bridge,'channel',None),
                          'origins':sorted(bridge.bindings) if bridge is not None else [],
                          'remote_health':'not_observed','custody':custody,'checkpoints':checkpoints},
            'qualification':{'owner_interoperability':'unqualified','atomic_idle_only_wake':'unqualified'}}


def operator_dashboard(app):
    return service_dashboard(app.state.settings, app.state.store, socket=app.state.socket,
                             bridge=app.state.federation_bridge, version=app.version,
                             started_at=getattr(app.state,'runtime_started',None))

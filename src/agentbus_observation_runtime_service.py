"""Observe authenticated API responses and reuse one existing worker slot for notices."""
import sqlite3
import time

import httpx

from src.agentbus_federation_slack_service import bounded_response


class ApiAccessObservation:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def observed_send(message):
            if (scope["type"] == "http" and message["type"] == "http.response.start"
                    and 200 <= message["status"] < 300 and scope.get("path", "").startswith(("/v1/", "/a2a"))
                    and scope.get("path") != "/v1/status"):
                store = getattr(getattr(scope.get("app"), "state", None), "store", None)
                observations = getattr(store, "observations", None)
                binding = scope.get("agentbus_access_scope")
                if observations is not None and binding is not None:
                    try:
                        observations.observe_scope(binding)
                    except PermissionError:
                        pass
                    except (sqlite3.Error, OSError, ValueError):
                        observations.last_error = "actual_access_observation_unavailable"
            await send(message)
        await self.app(scope, receive, observed_send)


class StaleNoticeWorker:
    def __init__(self, observations, client, settings):
        self.observations, self.client, self.settings = observations, client, settings

    def pending(self):
        return self.observations.next_notice() is not None

    async def submit_once(self, *, budget=1, deadline=None):
        if budget < 1:
            return 0
        deadline = time.monotonic() + 15 if deadline is None else deadline
        if time.monotonic() >= deadline:
            return 0
        item = self.observations.next_notice()
        if item is None or not self.observations.reserve_notice(item):
            return 0
        # A notice is reserved before submission. Lost responses and failures do
        # not blindly retry; only a fresh actual API observation permits a new episode.
        text = ("AgentBus stale-access notice: session " + item["session_id"] +
                " has no recent observed API access. Continuous monitoring and recipient work are unknown. "
                "This notice grants no wake, retry, deployment or execution authority.")
        payload = {"channel": item["channel"], "user": item["operator"], "text": text, "parse": "none"}
        state = "uncertain"
        try:
            response = await bounded_response(self.client, self.settings.slack_bot_token, "chat.postEphemeral", payload,
                max_bytes=16384, timeout_seconds=max(0.001, min(15, deadline - time.monotonic())))
            data = response.json()
            if isinstance(data, dict) and response.is_success and data.get("ok") is True:
                state = "submitted_private_notice"
            elif isinstance(data, dict) and data.get("ok") is False:
                state = "provider_rejected_no_retry"
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            pass
        self.observations.settle_notice(item, state)
        return 1

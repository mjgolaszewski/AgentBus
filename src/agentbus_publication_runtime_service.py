"""One bounded outbound publication step on the existing worker and HTTP client."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

import httpx

from src.agentbus_federation_slack_service import bounded_response


def retry_seconds(value):
    if value is None:
        return 60
    if re.fullmatch(r"[0-9]{1,9}", value):
        return min(86400, max(1, int(value)))
    return 86400  # An unparseable hint cannot authorize an early retry; drafts expire.


class PublicationWorker:
    def __init__(self, custody, client):
        self.custody, self.client = custody, client
        self.verified = {}

    async def submit_once(self, *, budget=1, deadline=None):
        deadline = time.monotonic() + 15 if deadline is None else deadline
        if not self.custody.coordination_verified or budget < 1 or time.monotonic() >= deadline:
            return 0
        for _ in range(8):
            request_id = self.custody.next_ready()
            if request_id is None:
                return 0
            candidate = self.custody.prepare(request_id)
            if candidate is not None:
                break
        else:
            return 0
        policy = self.custody.policy
        destination = policy.destinations[candidate["destination"]]
        installation = policy.installations[destination.installation]
        token = policy.token(installation.name)
        binding = (installation.team_id, installation.bot_id, hashlib.sha256(token.encode()).hexdigest())
        method = "auth.test" if self.verified.get(installation.name) != binding else "chat.postMessage"
        until = self.custody.provider_retry_at(installation, method)
        if until > self.custody.now():
            self.custody.defer_ready(request_id, failure="provider_verification_unavailable" if method == "auth.test" else "provider_rate_limited",
                                     seconds=min(86400, max(1, math.ceil(until - self.custody.now()))))
            return 0
        if self.verified.get(installation.name) != binding:
            try:
                response = await bounded_response(self.client, token, "auth.test", max_bytes=16384,
                                                  timeout_seconds=max(0.001, min(15, deadline - time.monotonic())))
                if response.status_code == 429 or response.status_code >= 500:
                    self.custody.defer_ready(request_id, failure="provider_verification_unavailable",
                                             seconds=retry_seconds(response.headers.get("Retry-After")),
                                             provider=(installation, "auth.test"))
                    return 1
                data = response.json()
                if (not response.is_success or not isinstance(data, dict) or data.get("ok") is not True
                        or data.get("team_id") != installation.team_id or data.get("bot_id") != installation.bot_id):
                    self.custody.fail_before_submission(request_id, "provider_binding_mismatch")
                else:
                    self.verified[installation.name] = binding
            except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
                self.custody.defer_ready(request_id, failure="provider_verification_unavailable", provider=(installation, "auth.test"))
            return 1
        row = self.custody.begin(request_id)
        if row is None:
            return 0
        payload = json.loads(row["payload"])
        text = payload["text"]
        try:
            response = await bounded_response(self.client, token, "chat.postMessage", payload, max_bytes=262144,
                                              timeout_seconds=max(0.001, min(15, deadline - time.monotonic())))
            if response.status_code == 429:
                seconds = retry_seconds(response.headers.get("Retry-After"))
                self.custody.defer_ready(request_id, failure="provider_rate_limited", seconds=seconds,
                                         provider=(installation, "chat.postMessage"))
                return 1
            data = response.json()
            if not response.is_success or not isinstance(data, dict):
                self.custody.settle(request_id, "uncertain", failure="provider_response_unverified",
                                     pause_provider=(installation, "chat.postMessage"))
            elif data.get("ok") is False:
                self.custody.settle(request_id, "failed", failure="provider_rejected")
            elif (data.get("ok") is not True or data.get("channel") != destination.channel
                    or not isinstance(data.get("ts"), str) or not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,20}", data["ts"])
                    or not isinstance(data.get("message"), dict) or data["message"].get("text") != text):
                self.custody.settle(request_id, "uncertain", failure="provider_response_unverified",
                                     pause_provider=(installation, "chat.postMessage"))
            else:
                self.custody.settle(request_id, "delivered", provider_ref=data["ts"])
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            self.custody.settle(request_id, "uncertain", failure="provider_outcome_unknown",
                                 pause_provider=(installation, "chat.postMessage"))
        return 1

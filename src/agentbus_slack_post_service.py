"""Post a validated AgentBus envelope to one configured Slack room."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException

from src.agentbus_presentation_service import visible_text
from src.agentbus_settings_service import Settings

if TYPE_CHECKING:
    from src.agentbus_service import SendMessage

SLACK_TS = r"^[0-9]{1,20}\.[0-9]{1,20}$"

class SlackPoster:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def authenticated_bot_id(self) -> str:
        try:
            response = await self.client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Slack bot identity verification failed") from None
        bot_id = data.get("bot_id") if response.is_success and isinstance(data, dict) else None
        if (
            not isinstance(data, dict) or data.get("ok") is not True
            or not isinstance(bot_id, str) or not re.fullmatch(r"B[A-Z0-9]+", bot_id)
        ):
            raise RuntimeError("Slack bot identity verification failed")
        return bot_id

    async def post(self, message: SendMessage, channel: str | None = None) -> str:
        channel = channel or self.settings.slack_channel
        from src.agentbus_service import encode_envelope

        label = f"{message.sender} → {message.recipient} · {message.kind} · {message.audience}"
        if message.repo:
            label += f" · {message.repo}"
        blocks: list[dict[str, object]] = [
            {"type": "context", "elements": [{"type": "plain_text", "text": label, "emoji": False}]}
        ]
        display = visible_text(message.text)
        blocks.extend({"type": "section", "text": {"type": "plain_text", "text": display[i:i + 3000], "emoji": False}}
                      for i in range(0, len(display), 3000))
        payload = {"channel": channel, "text": encode_envelope(message), "blocks": blocks,
                   "parse": "none", "mrkdwn": False, "unfurl_links": False, "unfurl_media": False}
        if message.thread_ts:
            payload["thread_ts"] = message.thread_ts
        try:
            response = await self.client.post("https://slack.com/api/chat.postMessage", json=payload,
                                              headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"})
        except httpx.TimeoutException:
            raise HTTPException(504, "Slack timed out; delivery is uncertain. Check the channel before retrying.") from None
        except httpx.RequestError:
            raise HTTPException(502, "Slack request failed; delivery may be uncertain. Check the channel before retrying.") from None
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "1")
            retry = retry if re.fullmatch(r"[0-9]{1,6}", retry) else "1"
            raise HTTPException(429, "Slack rate limit reached", headers={"Retry-After": retry})
        if not response.is_success:
            raise HTTPException(502, "Slack request failed; delivery may be uncertain. Check the channel before retrying.")
        try:
            data = response.json()
        except ValueError:
            raise HTTPException(502, "Slack returned an invalid response; delivery is uncertain") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise HTTPException(502, "Slack rejected the message; check app permissions and channel membership")
        ts = data.get("ts")
        if not isinstance(ts, str) or not re.fullmatch(SLACK_TS, ts) or data.get("channel") != channel:
            raise HTTPException(502, "Slack returned an unexpected message response; delivery is uncertain")
        return ts

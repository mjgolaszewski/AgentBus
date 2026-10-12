"""Compact agent-facing directory query with optional explicit refresh."""

from __future__ import annotations

import json
import time

from src.agentbus_transport_client import ClientError, api, profile_session_token


def peers(ns, values: dict[str, str]) -> int:
    if ns.refresh:
        from src.agentbus_client import load_profile, resolve_identity

        identity = resolve_identity(ns.identity)
        token = profile_session_token(load_profile(values, identity))
        if not token:
            raise ClientError("Join this chat before requesting a peer-directory refresh.")
        before = api(values, "/v1/peers")
        prior = {source["prefix"]: source["observed_at"] for source in before["sources"]
                 if source["source"] == "peer"}
        api(values, "/v1/peers/refresh", {"identity": identity}, session_token=token)
        # Wait below the model boundary for the configured peers' Socket Mode replies.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            result = api(values, "/v1/peers")
            sources = [source for source in result["sources"] if source["source"] == "peer"]
            if sources and all(source["observed_at"] != prior.get(source["prefix"])
                               for source in sources):
                break
            time.sleep(0.25)
    else:
        result = api(values, "/v1/peers")
    if ns.refresh:
        result["refresh_pending"] = [source["prefix"] for source in result["sources"]
                                     if source["source"] == "peer" and
                                     source["observed_at"] == prior.get(source["prefix"])]
    if ns.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"# this service: {result['sources'][0]['prefix'] or 'unconfigured'} "
              f"({result['local_bot_id']})")
        print("ROUTE\tNAME\tSOURCE\tOBSERVED")
        for agent in result["agents"]:
            print(f"{agent['route']}\t{agent['display_name']}\t{agent['source_prefix']}"
                  f"\t{agent.get('observed_at', 'local')}")
        for source in result["sources"]:
            if source["source"] == "peer" and source["observed_at"] is None:
                print(f"# {source['prefix']}: no snapshot received; run peers --refresh")
            elif source["prefix"] in result.get("refresh_pending", []):
                print(f"# {source['prefix']}: no fresh reply yet; prior snapshot remains observed-only")
            elif source["complete"] is False:
                print(f"# {source['prefix']}: snapshot incomplete")
    return 0

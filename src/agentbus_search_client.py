"""Session-scoped mailbox queries over the existing local API."""

import json
from urllib.parse import urlencode

from src.agentbus_scope_client import scoped_api


def search_command(ns, project, values):
    from src.agentbus_client import load_profile, resolve_identity

    profile = load_profile(values, resolve_identity(ns.identity), save_migration=False)
    query = {"query": ns.query, "limit": ns.limit}
    if ns.after:
        query["after"] = ns.after
    result = scoped_api(values, "/v1/search?" + urlencode(query), profile)
    print(json.dumps(result, indent=2))
    return 0

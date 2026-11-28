"""Bounded unrouted context through the existing exact-session history API."""
from __future__ import annotations

import json

from src.agentbus_client import checked_profile, resolve_identity
from src.agentbus_scope_client import scoped_api
from src.agentbus_transport_client import ClientError


def attention_command(ns, values, *, api_call=None):
    profile, info = checked_profile(values, resolve_identity(ns.identity))
    high = info.get('high_water_cursor')
    if type(high) is not int or high < 0:
        raise ClientError('Attention history high-water is unknown; no cursor changed')
    after = max(0, high - 200)
    page = scoped_api(values, f'/v1/messages?after={after}&limit=200', profile, api_call=api_call)
    rows = page.get('messages')
    if (not isinstance(rows, list) or len(rows) > 200
            or any(not isinstance(row, dict) or type(row.get('cursor')) is not int
                   or row['cursor'] <= after for row in rows)
            or [row['cursor'] for row in rows] != sorted({row['cursor'] for row in rows})):
        raise ClientError('Attention context page is invalid; evidence retained')
    notices = [row for row in rows if row.get('audience') == 'unrouted']
    result = {'messages':notices[-20:], 'inbox_id':profile['inbox_id'], 'channel':profile['channel'],
              'sampled_high_water':high, 'scan_limit':200, 'return_limit':20,
              'partial':after > 0 or bool(page.get('has_more')) or len(notices) > 20,
              'observation_mode':'historical_context', 'presentation_advanced':False,
              'semantic_ack_advanced':False,
              'guidance':'Unrouted context has no inferred recipient or authority. Claim eligible work explicitly.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

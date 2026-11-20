"""Bounded operator-provided contact hints; never resource admission or live probes."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from uuid import UUID

from src.agentbus_message_assurance_service import canonical_route


@dataclass(frozen=True)
class RecoveryContact:
    channel: str
    route: str
    observed_at: float | None
    expires_at: float | None
    receipt_position: int | None
    service_epoch: str | None
    host_epoch: str | None

    def __post_init__(self):
        row=vars(self)
        if (not isinstance(self.channel,str) or not re.fullmatch(r'[CG][A-Z0-9]{1,79}',self.channel)
                or not isinstance(self.route,str) or not canonical_route(self.route)):
            raise ValueError('Invalid recovery contact')
        seen,expires=row['observed_at'],row['expires_at']
        if (any(v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0) for v in (seen,expires))
                or (seen is None)!=(expires is None) or seen is not None and not 0<expires-seen<=7*86400):
            raise ValueError()
        position=row['receipt_position']
        if position is not None and (type(position) is not int or not 0<position<2**63):
            raise ValueError()
        for key in ('service_epoch','host_epoch'):
            v=row[key]
            if v is not None and (not isinstance(v,str) or len(v)!=36 or str(UUID(v))!=v):
                raise ValueError()


def decode_contacts(raw, channels):
    if raw is None or raw == '':
        return ()
    try:
        if not isinstance(raw,str) or len(raw.encode()) > 16384:
            raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict) or set(value)!={'version','contacts'} or type(value['version']) is not int or value['version']!=1:
            raise ValueError()
        if not isinstance(value['contacts'],list) or len(value['contacts'])>16:
            raise ValueError()
        result=[]
        fields={'channel','route','observed_at','expires_at','receipt_position','service_epoch','host_epoch'}
        for row in value['contacts']:
            if (not isinstance(row,dict) or set(row)!=fields or not isinstance(row['channel'],str)
                    or row['channel'] not in channels or not isinstance(row['route'],str) or not canonical_route(row['route'])):
                raise ValueError()
            result.append(RecoveryContact(**row))
        if len({row.channel for row in result})!=len(result):
            raise ValueError()
        return tuple(result)
    except (ValueError,TypeError,KeyError,RecursionError,OverflowError,AttributeError):
        raise ValueError('Invalid bounded recovery-contact configuration; no contact authority was installed') from None


def contact_snapshot(settings, store, channel, *, instant=None):
    rows=settings.recovery_contacts
    row=next((row for row in rows if row.channel==channel),None)
    if row is None:
        return {'configured':False,'state':'unknown','resource_admission':False}
    now=time.time() if instant is None else instant
    with store._lock:
        available=channel in store.channels.enabled and store.channels.local_target(channel,row.route)
        state=('unavailable' if not available else 'unknown_freshness' if row.observed_at is None or row.expires_at is None
               else 'future_report' if now<row.observed_at else 'expired_report' if now>=row.expires_at else 'unexpired_report')
        receipt=None
        if available and row.receipt_position is not None:
            receipt=store._db.execute('SELECT 1 FROM channel_positions p JOIN messages m ON m.cursor=p.message_id '
                                     'AND m.channel=p.channel WHERE p.channel=? AND p.position=?',
                                     (channel,row.receipt_position)).fetchone()
        return {'configured':True,'state':state,'contact':row.route if available else None,
                'reported_at':row.observed_at,'expires_at':row.expires_at,
                'receipt':{'position':row.receipt_position if available else None,
                           'state':'retained_reference' if receipt else 'unavailable' if row.receipt_position else 'not_supplied'},
                'authorship':'operator_configuration','service_epoch':row.service_epoch,'host_epoch':row.host_epoch,
                'epoch_validation':'unverified_or_unknown','evidence_role':'non_authorizing_contact_hint',
                'resource_admission':False,'continuous_monitoring':False}

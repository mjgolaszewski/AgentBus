"""Resolve service-owned channel authority before data lookup or projection."""
from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, Query, Request, Response

from src.agentbus_participation_store_service import StoppedMessageSession

IDENTIFIER=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$'
SLACK_TS=r'^[0-9]{1,20}\.[0-9]{1,20}$'


def resolve_principal(request, route=None, token=None, *, info=False):
    if any(key in request.query_params for key in ('channel','channel_id')) or request.headers.get('X-AgentBus-Channel'):
        raise HTTPException(422,'Agent channel selection is not supported')
    token=token if token is not None else request.headers.get('X-AgentBus-Session-Token')
    try:
        principal=request.app.state.store.channels.principal(token,route,allow_joining=info)
    except StoppedMessageSession:
        raise HTTPException(423,'Session is stopped') from None
    except PermissionError:
        raise HTTPException(403,'Matching session scope required') from None
    assertion=request.headers.get('X-AgentBus-Inbox-ID')
    if (assertion is None and not info) or (assertion is not None and assertion!=principal.inbox_id):
        raise HTTPException(403,'Matching session scope required')
    if not info and request.app.state.store.lifecycle.enabled and request.headers.get('X-AgentBus-Lifecycle-Version') != '1':
        raise HTTPException(426,'Lifecycle-capable client required; refresh the client before accessing messages')
    return principal


def serialize_message(store,channel,message):
    result=message.model_dump()
    result['cursor']=store.channels.public_cursor(channel,message.cursor)
    if message.reply_to_cursor is not None:
        result['reply_to_cursor']=store.channels.public_cursor(channel,message.reply_to_cursor)
    return result


def serialize_page(store,channel,page,after=0):
    result={'messages':[serialize_message(store,channel,m) for m in page.messages],
        'next_cursor':max(after,store.channels.public_cursor(channel,page.messages[-1].cursor)) if page.has_more
            else max(after,store.channels.high_water(channel)), 'has_more':page.has_more}
    if store.lifecycle.enabled:
        result['retired_history']=store.retention.has_retired_positions(channel, after, result['next_cursor'])
    return result


def api_read(request:Request,after:Annotated[int,Query(ge=0)]=0,
             recipient:Annotated[str|None,Query(pattern=IDENTIFIER)]=None,
             limit:Annotated[int,Query(ge=1,le=200)]=100,
             thread_ts:Annotated[str|None,Query(pattern=SLACK_TS)]=None):
    principal=resolve_principal(request)
    store=request.app.state.store
    with store.snapshot():
        page=store.read(principal.channel,store.channels.after_cursor(principal.channel,after),recipient,limit,thread_ts)
        return serialize_page(store,principal.channel,page,after)


def api_info(request:Request,response:Response):
    principal=resolve_principal(request,info=True)
    store=request.app.state.store
    response.headers['X-AgentBus-Delivery-Version']='2' if store.lifecycle.enabled else '1'
    result={'protocol_version':2,'chat_id':principal.chat_id,'session_id':principal.session_id,'inbox_id':principal.inbox_id,'channel':principal.channel,
        'high_water_cursor':request.app.state.store.channels.high_water(principal.channel),
        'features':['profiles','stateless-cursors','explicit-routing','parent-replies','session-required',
                    'channel-isolation-v1','channel-local-cursors-v1','authored-request-outcome-v1','mandatory-announcements-v1','slack-enrollment-approval-v1']}
    if store.lifecycle.enabled:
        epoch=store.lifecycle.epoch(principal.channel)
        result['features'].append('message-lifecycle-v1')
        result['lifecycle']={'version':1,'request_epoch':epoch['epoch'] if epoch else None,
                             'pickup_seconds':store.lifecycle.policy.pickup_seconds}
    return result


def api_inbox(request:Request,identity:Annotated[str,Query(pattern=IDENTIFIER)],
              after:Annotated[int,Query(ge=0)]=0,limit:Annotated[int,Query(ge=1,le=200)]=100,
              thread_ts:Annotated[str|None,Query(pattern=SLACK_TS)]=None,context:bool=False,
              x_agentbus_session_token:Annotated[str|None,Header()]=None):
    principal=resolve_principal(request,identity,x_agentbus_session_token)
    store=request.app.state.store
    with store.snapshot():
        page=store.inbox(principal.channel,identity,store.channels.after_cursor(principal.channel,after),limit,thread_ts,context)
        return serialize_page(store,principal.channel,page,after)


def claim_message(request,cursor,claim,token):
    principal=resolve_principal(request,claim.identity,token)
    store=request.app.state.store
    internal=store.channels.internal_cursor(principal.channel,cursor)
    if internal is None:
        raise HTTPException(404,'Message not found')
    try:
        result=store.claim(principal.channel,internal,claim.identity,session_token=token)
    except StoppedMessageSession:
        raise HTTPException(423,'Session is stopped') from None
    except PermissionError:
        raise HTTPException(403,'Matching session scope required') from None
    except KeyError:
        raise HTTPException(404,'Message not found') from None
    except ValueError:
        raise HTTPException(409,'Message cannot be claimed') from None
    if result.identity!=claim.identity:
        raise HTTPException(409,'Message already claimed',headers={'X-AgentBus-Claimed-By':result.identity})
    return {**result.model_dump(),'cursor':cursor}


def require_target(store,channel,message,bridge=None):
    if message.recipient=='all':
        return
    if message.recipient.startswith('slack:') and message.reply_to_cursor is not None:
        parent=store.message(channel,message.reply_to_cursor)
        if parent is not None and parent.sender==message.recipient and parent.sender_assurance=='slack-human':
            return
    if store.channels.local_target(channel,message.recipient):
        return
    from src.agentbus_message_assurance_service import route_parts
    parts=route_parts(message.recipient)
    if bridge is not None and bridge.channel==channel and parts and any(
            binding.route_prefix==parts[0] for binding in bridge.bindings.values()):
        return
    raise HTTPException(403,'Destination unavailable in this scope')


def provider_message(store, channel, message):
    if message.reply_to_cursor is None:
        return message
    return message.model_copy(update={'reply_to_cursor':store.channels.public_cursor(channel,message.reply_to_cursor)})


def assert_session_scope(request,session_id,secret):
    if any(key in request.query_params for key in ('channel','channel_id')) or request.headers.get('X-AgentBus-Channel'):
        raise HTTPException(422,'Agent channel selection is not supported')
    if not secret or len(secret)>512:
        raise HTTPException(403,'Matching session scope required')
    store=request.app.state.store
    try:
        session=store.participation.authorize_session(session_id,secret)
        channel=store.channels.channel_for_chat(session['chat_id'])
        inbox=store.channels.inbox_id(channel)
    except (PermissionError,KeyError):
        raise HTTPException(403,'Matching session scope required') from None
    if request.headers.get('X-AgentBus-Inbox-ID')!=inbox:
        raise HTTPException(403,'Matching session scope required')

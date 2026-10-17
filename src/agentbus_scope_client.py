"""Explicit scope for disposable client projections; never membership authority."""
from src.agentbus_transport_client import ClientError

FIELDS=('service_url','inbox_id','channel','chat_id','session_id')


def scope(profile):
    result={key:profile.get(key) for key in FIELDS[:-1]}
    result['session_id']=(profile.get('participation') or {}).get('session_id')
    if any(not isinstance(value,str) or not value for value in result.values()):
        raise ClientError('Joined profile needs verified service channel and inbox scope')
    return result


def verify_projection(state,profile):
    expected=scope(profile)
    if any(state.get(key)!=value for key,value in expected.items()):
        raise ClientError('Projection scope differs from the channel inbox binding; evidence retained')


def scoped_api(values,path,profile,payload=None,*,api_call=None,**kwargs):
    from src.agentbus_transport_client import api, profile_session_token
    expected=scope(profile)
    if expected['service_url']!=values.get('AGENTBUS_URL'):
        raise ClientError('Profile service scope differs; no automatic channel switch')
    kwargs['session_token']=profile_session_token(profile)
    kwargs['inbox_id']=expected['inbox_id']
    call=api_call or api
    return call(values,path,**kwargs) if payload is None else call(values,path,payload,**kwargs)

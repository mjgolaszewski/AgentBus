"""Private identity controls; buttons locate immutable owner-held previews only."""
from __future__ import annotations

from src.agentbus_identity_management_service import IdentityManagement
from src.agentbus_presentation_service import section_blocks
from src.agentbus_slack_ui_service import OperatorAction, OperatorReply, action_button


def _safe(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', 'ˋ')


def _reply(text, buttons=(), *, outcome='observed', receipt=None):
    blocks = section_blocks(text)
    if buttons:
        blocks.append({'type':'actions','elements':list(buttons)})
    return OperatorReply(text, blocks, outcome=outcome, receipt=receipt)


def identity_list(owner, actor, channel, *, page=0, retired=False):
    data = owner.identities(actor, channel, page=page, include_retired=retired)
    text = '*Identity manager — '+_safe(data['location'])+'*\nThis channel only. Select an identity; no action has been applied.'
    blocks = section_blocks(text)
    for row in data['identities']:
        label = '*'+_safe(row['current_route'])+'*\n'+('Retired' if row['retired_at'] else 'Registered')
        blocks.extend(section_blocks(label))
        blocks.append({'type':'actions','elements':[
            action_button('Manage identity',OperatorAction('identity_detail',channel,row['chat_id']))]})
    kind = 'identity_retired_page' if retired else 'identity_page'
    nav = []
    if page:
        nav.append(action_button('Previous',OperatorAction(kind,channel,page=page-1)))
    if data['has_more']:
        nav.append(action_button('Next',OperatorAction(kind,channel,page=page+1)))
    nav.append(action_button('Current identities' if retired else 'Include retired',
                             OperatorAction('identity_page' if retired else 'identity_retired_page',channel)))
    blocks.append({'type':'actions','elements':nav})
    return OperatorReply(text,blocks,outcome='observed')


def identity_detail(owner, actor, channel, chat_id, command):
    data = owner.detail(actor,channel,chat_id)
    history = data['history']
    text = ('*'+_safe(data['route'])+'*\n'
            +('Retired' if data['retired'] else 'Registered')+' · non-stopped sessions `'+str(data['sessions'])+'`\n'
            +'Known authored local messages `'+str(history['retained'])+'`; deletion eligibility not assessed.\n\n'
            +'Rename: `'+_safe(command)+' identities rename UNIQUE-FRAGMENT new-name`\n'
            +'Remove revokes AgentBus access; it does not stop a host or acknowledge work.\n'
            +'History cleanup retains all reference/ACK/age pins. Slack, remote copies, updates and backups stay intact.')
    buttons = [action_button('Preview remove',OperatorAction('identity_retire',channel,chat_id))]
    if history['supported']:
        buttons.append(action_button('Preview remove + history',OperatorAction('identity_history',channel,chat_id)))
    buttons.append(action_button('Back',OperatorAction('identity_page',channel)))
    return _reply(text,buttons)


def identity_preview(owner, actor, channel, chat_id, action, *, name=None):
    data = owner.preview(actor,channel,chat_id,action,name=name)
    intent, history = data['intent'], data['history']
    text = ('*Confirm identity action: '+_safe(action)+'*\nIdentity: '+_safe(data['route'])
            +'\nSessions in retained snapshot `'+str(len(intent['session_ids']))+'`'
            +'\nExact history batch `'+str(len(intent['positions']))+'`; retained authored messages `'+str(history['retained'])+'`')
    if name is not None:
        text += '\nNew name: '+_safe(intent['new_route'])+'; repository, identity and channel stay unchanged.'
    text += ('\n\nThis preview expires in ten minutes. Current authorization and exact population are rechecked. '
             'No recipient ACK, host termination, Slack/remote/backup deletion or safe provider retry is inferred.\n'
             +_safe(history['pins']))
    return _reply(text,[action_button('Confirm exact action',OperatorAction('identity_confirm',channel,
                     intent['request_id'],digest=data['digest'])),
                       action_button('Cancel / back',OperatorAction('identity_detail',channel,chat_id))],outcome='preview',receipt=intent['request_id'])


def identity_result(owner, actor, channel, request_id):
    data = owner.receipt(actor,channel,request_id)
    if data['state'] != 'committed':
        return _reply('Identity preview remains unapplied'+(' and expired.' if data['expired'] else '.'),
                      [action_button('Back',OperatorAction('identity_page',channel))],receipt=request_id)
    text = ('*Identity action recorded*\n'+_safe(data['action'])+' · '+_safe(data['route'])
            +'\nSessions revoked `'+str(data['sessions_revoked'])+'`'
            +'\nEligible local history removed `'+str(data['history']['removed'])+'`; retained `'+str(data['history']['retained'])+'`'
            +'\nNo semantic acknowledgement or host execution is inferred. Accepted delivery custody remains authoritative.')
    return _reply(text,[action_button('Inspect receipt',OperatorAction('identity_receipt',channel,request_id)),
                       action_button('Back',OperatorAction('identity_page',channel))],outcome='committed',receipt=request_id)


def identity_command(parts,settings,store,actor,channel,command):
    owner = IdentityManagement(store,settings)
    try:
        if len(parts) == 1:
            return identity_list(owner,actor,channel)
        if len(parts) == 2 and parts[1] == 'retired':
            return identity_list(owner,actor,channel,retired=True)
        # The outer command parser keeps the tail intact, so names never become arbitrary shell syntax.
        words = ' '.join(parts).split()
        if len(words) == 4 and words[1] == 'rename':
            chat_id = owner.resolve(actor,channel,words[2])
            return identity_preview(owner,actor,channel,chat_id,'rename',name=words[3])
        if len(words) == 3 and words[1] in {'remove','remove-history','show'}:
            chat_id = owner.resolve(actor,channel,words[2])
            if words[1] == 'show':
                return identity_detail(owner,actor,channel,chat_id,command)
            return identity_preview(owner,actor,channel,chat_id,
                                    'retire_with_history' if words[1]=='remove-history' else 'retire')
        return 'Use `'+command+' identities`, then private preview/confirmation buttons. Rename: `'+command+' identities rename UNIQUE-FRAGMENT new-name`.'
    except (ValueError,PermissionError) as exc:
        return str(exc)


def identity_interaction(action,settings,store,actor,channel):
    owner = IdentityManagement(store,settings)
    if action.kind in {'identity_page','identity_retired_page'}:
        return identity_list(owner,actor,channel,page=action.page,retired=action.kind=='identity_retired_page')
    if action.kind == 'identity_detail':
        return identity_detail(owner,actor,channel,action.target,settings.slack_command)
    if action.kind in {'identity_retire','identity_history'}:
        return identity_preview(owner,actor,channel,action.target,
                                'retire_with_history' if action.kind=='identity_history' else 'retire')
    if action.kind == 'identity_confirm':
        owner.perform(actor,channel,action.target,action.digest)
    return identity_result(owner,actor,channel,action.target)

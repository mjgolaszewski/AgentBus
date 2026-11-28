"""Private sent/reply and unrouted-context queries; never mutate custody."""
from __future__ import annotations

import re

from src.agentbus_presentation_service import section_blocks
from src.agentbus_slack_ui_service import OperatorAction, OperatorReply, action_button, navigation


def _link(channel, timestamp):
    if not isinstance(timestamp, str) or not re.fullmatch(r'[0-9]{1,20}\.[0-9]{6}', timestamp):
        return None
    return f'https://app.slack.com/archives/{channel}/p{timestamp.replace(".", "")}'


def sent_reply(store, channel, actor, *, page=0, target=None, open_thread=False):
    """Actor comes only from the verified Socket Mode operator principal."""
    with store._lock:
        if target is not None:
            if not re.fullmatch(r'[1-9][0-9]{0,18}', target):
                raise ValueError('Choose a message from Sent.')
            cursor = store.channels.internal_cursor(channel, int(target))
            row = store._db.execute("SELECT * FROM messages WHERE channel=? AND cursor=? "
                "AND sender=? AND sender_assurance='slack-human' AND audience='direct'",
                (channel, cursor, f'slack:{actor}')).fetchone()
            if row is None:
                raise ValueError('Message unavailable in your Sent view for this channel.')
            parent = store._message(row)
            thread = parent.thread_ts or row['slack_ts']
            link = _link(channel, thread)
            if open_thread:
                text = f'Provider thread: {link}' if link else 'Provider thread link unavailable; delivery not inferred.'
                return OperatorReply(text, section_blocks(text))
            candidates = store._db.execute('SELECT * FROM messages WHERE channel=? AND cursor>? '
                'AND thread_ts=? ORDER BY cursor DESC LIMIT 201',
                (channel, row['cursor'], thread)).fetchall()
            replies = []
            for candidate in candidates[:200]:
                item = store._message(candidate)
                proved = candidate['sender_assurance'] == 'session' or store._db.execute(
                    'SELECT 1 FROM federation_inbox_projection WHERE cursor=?',
                    (candidate['cursor'],)).fetchone() is not None
                if (proved and item.reply_to_cursor == parent.cursor and item.audience == 'direct'
                        and item.recipient == f'slack:{actor}'):
                    replies.append(item)
            text = f'*Replies to sent message `{target}`*\n{parent.text[:240]}'
            if not replies:
                text += '\nNo authenticated linked reply in this bounded window. Remote visibility may be unavailable.'
            for item in replies[:20]:
                position = store.channels.public_cursor(channel, item.cursor)
                text += f'\n\n`{position}` · {item.sender}\n{item.text[:1000]}'
            if len(candidates) > 200 or len(replies) > 20:
                text += '\nPartial coverage: more retained thread context or replies exist.'
            text += '\nRead-only; no work completion or ACK inferred.'
            return OperatorReply(text, section_blocks(text))
        rows = store._db.execute("SELECT * FROM messages WHERE channel=? AND sender=? "
            "AND sender_assurance='slack-human' AND audience='direct' ORDER BY cursor DESC LIMIT 41",
            (channel, f'slack:{actor}')).fetchall()
        controls = navigation('sent_page', '', page, min(40, len(rows)), 4, channel)
        text = '*Your sent messages*\nLatest 40 retained messages in this channel; older/retired history may be unavailable.'
        blocks = section_blocks(text)
        for row in rows[page * 4:min(40, (page + 1) * 4)]:
            item = store._message(row)
            position = store.channels.public_cursor(channel, item.cursor)
            states = dict(store._db.execute('SELECT state,count(*) FROM lifecycle_targets '
                'WHERE channel=? AND cursor=? GROUP BY state', (channel, row['cursor'])).fetchall())
            summary = (f'`{position}` → *{item.recipient}*\n{item.received_at}\n{item.text[:300]}\n'
                       f'Retained message; recipient observations: {states or "unavailable"}. '
                       'Replies are separate; completion/ACK not inferred.')
            text += '\n\n' + summary
            blocks.extend(section_blocks(summary))
            buttons = [action_button('View replies', OperatorAction('sent_replies', channel, str(position)))]
            link = _link(channel, item.thread_ts or row['slack_ts'])
            if link:
                button = action_button('Open thread', OperatorAction('sent_open', channel, str(position)))
                button['url'] = link
                buttons.insert(0, button)
            blocks.append({'type': 'actions', 'elements': buttons})
        blocks.extend(controls)
        if not rows:
            text += '\nNo verified human-authored messages in this view.'
        return OperatorReply(text, blocks)


def attention_reply(store, channel, *, page=0):
    """Unrouted content is context, regardless of apparent sender/instructions."""
    with store._lock:
        rows = store._db.execute("SELECT * FROM messages WHERE channel=? AND audience='unrouted' "
            'ORDER BY cursor DESC LIMIT 41', (channel,)).fetchall()
        controls = navigation('attention_page', '', page, min(40, len(rows)), 4, channel)
        text = ('*Unrouted attention context*\nLatest 40 retained notices, not an actionable inbox. '
                'No recipient inferred. Legacy/unconfirmed content supplies no authority; '
                'claim explicitly before acting on an eligible message.')
        for row in rows[page * 4:min(40, (page + 1) * 4)]:
            item = store._message(row)
            position = store.channels.public_cursor(channel, item.cursor)
            text += f'\n\n`{position}` · {item.sender} · assurance `{item.sender_assurance}`\n{item.received_at}\n{item.text[:600]}'
        if not rows:
            text += '\nNo notices in this retained view; absence does not prove continuous ingress.'
        if len(rows) > 40:
            text += '\nPartial retained window; older notices exist.'
        return OperatorReply(text, [*section_blocks(text), *controls])

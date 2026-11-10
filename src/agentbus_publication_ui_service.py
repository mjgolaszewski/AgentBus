"""Private destination-authorized publication review; buttons bind immutable drafts."""
from __future__ import annotations

from src.agentbus_presentation_service import section_blocks
from src.agentbus_slack_ui_service import OperatorAction, OperatorReply, action_button


def publication_card(row, channel):
    header = ("*Client publication — " + row["destination"] + "*\nState `" + row["state"] + "`\n"
              "This deliberately authored update has separate publication authority; bus chatter is never forwarded.")
    binding = row["destination_binding"]
    header += "\nAuthor: `" + row["author"] + "`; workspace `" + binding["team_id"] + "`; channel `" + binding["channel"] + "`."
    blocks = section_blocks(header)
    blocks.extend(row["payload_blocks"])
    buttons = [action_button("Publication result", OperatorAction("publication_results", channel, row["publication_id"]))]
    if row["can_approve"]:
        buttons.insert(0, action_button("Approve exact update", OperatorAction("publication_approve", channel,
                                                                              row["publication_id"], digest=row["digest"])))
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


def publication_review(owner, actor, channel, *, page=0):
    result = owner.list_operator(actor, channel, page=page)
    text = f"Client publications — page `{result['page'] + 1}/{result['pages']}`. Review the exact update before approving."
    blocks = section_blocks(text)
    for row in result["publications"]:
        summary = f"*{row['destination']}* — `{row['state']}`\nAuthor `{row['author']}`. Review the full retained update before approval."
        blocks.extend(section_blocks(summary))
        blocks.append({"type": "actions", "elements": [action_button("Review exact update",
            OperatorAction("publication_results", channel, row["publication_id"]))]})
    buttons = []
    for label, number in (("Previous", page - 1), ("Refresh", page), ("Next", page + 1)):
        if 0 <= number < result["pages"]:
            buttons.append(action_button(label, OperatorAction("publication_page", channel, page=number)))
    blocks.append({"type": "actions", "elements": buttons})
    return OperatorReply(text, blocks)


def publication_interaction(owner, action, actor, channel, *, wake=None):
    if action.kind == "publication_page":
        return publication_review(owner, actor, channel, page=action.page)
    row = owner.operator_status(action.target, actor, channel)
    if action.kind == "publication_approve":
        owner.approve(action.target, actor, action.digest)
        row = owner.operator_status(action.target, actor, channel)
        if wake is not None:
            wake.set()
    text = (f"Client publication: {row['state']}. Provider reference: `{row['provider_ref'] or 'not observed'}`. "
            "Approval is not provider delivery; client replies create no AgentBus task.")
    if row["failure"]:
        text += " Recovery observation: " + row["failure"] + "."
    return OperatorReply(text, [*section_blocks(text), *publication_card(row, channel)],
                         outcome="committed" if action.kind == "publication_approve" else "accepted", receipt=action.target)

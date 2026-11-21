"""Pure helpers for AI action approval routing and batch outcomes."""

from __future__ import annotations

from typing import Any


def approval_recipient_ids(guild: Any, approval_role_id: int | str | None,
                           bot_owner_id: int | str | None) -> set[int]:
    """Return unique human approver IDs: guild owner, bot owner, and approval-role members."""
    recipients: set[int] = set()

    for raw_id in (getattr(guild, "owner_id", None), bot_owner_id):
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            recipients.add(user_id)

    try:
        role_id = int(approval_role_id)
    except (TypeError, ValueError):
        role_id = 0

    if role_id > 0 and guild is not None:
        role = guild.get_role(role_id)
        if role is not None:
            for member in getattr(role, "members", ()):
                if getattr(member, "bot", False):
                    continue
                try:
                    user_id = int(member.id)
                except (AttributeError, TypeError, ValueError):
                    continue
                if user_id > 0:
                    recipients.add(user_id)

    return recipients


def batch_action_status(success_count: int, total_count: int) -> str:
    """Represent partial success honestly instead of labelling it completed."""
    if total_count <= 0 or success_count <= 0:
        return "failed"
    if success_count >= total_count:
        return "completed"
    return "partial"

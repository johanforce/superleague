# -*- coding: utf-8 -*-
"""Human-readable economy inbox helpers."""


def player_display_name(state: dict, player_id) -> str:
    pid = str(player_id or "")
    player = (state.get("players") or {}).get(pid)
    if player:
        return str(player.get("common_name") or player.get("name") or pid)
    for key in ("generated_players", "retired_players", "academy_alumni"):
        for row in state.get(key, []) or []:
            if str(row.get("player_id", row.get("id", ""))) == pid:
                return str(row.get("common_name") or row.get("name") or pid)
    return pid or "Cầu thủ không xác định"


def format_economy_notification(item: dict, state: dict) -> str:
    message = str(item.get("message", "Thông báo tài chính."))
    # Resolve raw player IDs left by older notifications, without changing stored data.
    pid = item.get("player_id")
    if pid:
        message = message.replace(str(pid), player_display_name(state, pid))
    return message

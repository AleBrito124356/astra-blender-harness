"""Keep resumed tool conversations valid without replaying uncertain operations."""

import copy


def repair_history(messages, vision=True):
    result = []
    pending = {}
    for original in copy.deepcopy(messages):
        role = original.get("role")
        if role != "tool" and pending:
            for call_id in pending:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": "Execution outcome was not recorded before interruption. Inspect the current "
                        "scene before deciding what to do. Do not automatically repeat this operation.",
                    }
                )
            pending = {}
        if role == "assistant":
            pending = {call["id"]: call for call in original.get("tool_calls") or []}
        if role == "tool":
            call_id = original.get("tool_call_id")
            if call_id not in pending:
                continue
            pending.pop(call_id)
        content = original.get("content")
        if not vision and isinstance(content, list):
            original["content"] = [
                b for b in content if b.get("type") != "image_url"
            ] or "Image omitted: vision disabled."
        result.append(original)
    for call_id in pending:
        result.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "Outcome unknown after interruption. Inspect the scene; do not replay automatically.",
            }
        )
    return result

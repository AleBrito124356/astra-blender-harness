from astra_blender.history import repair_history


def test_interrupted_batch_is_repaired_without_replaying_actions():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "saved"},
        {"role": "user", "content": "continue"},
    ]
    fixed = repair_history(messages)
    assert [m["role"] for m in fixed] == ["assistant", "tool", "tool", "user"]
    assert fixed[2]["tool_call_id"] == "b"
    assert "not automatically repeat" in fixed[2]["content"]
    assert len(messages) == 3


def test_trailing_pending_and_orphan_results():
    fixed = repair_history(
        [
            {"role": "tool", "tool_call_id": "orphan", "content": "unknown"},
            {"role": "assistant", "tool_calls": [{"id": "pending"}]},
        ]
    )
    assert len(fixed) == 2 and fixed[1]["tool_call_id"] == "pending"


def test_resuming_with_text_model_removes_images_but_keeps_text():
    fixed = repair_history(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "scene"},
                    {"type": "image_url", "image_url": {"url": "data:image/png,x"}},
                ],
            }
        ],
        vision=False,
    )
    assert fixed[0]["content"] == [{"type": "text", "text": "scene"}]

"""Perception text: what a model reads instead of a picture. Owned by WS1.

INTERFACE PINNED BY THE FOUNDATION. WS6 (the loop) and WS4 (the judge) code
against these four signatures; WS1 replaces the bodies with the real card
and diff (docs/sight-card.md). The bodies below are deliberately simple but
working, so callers can be written and tested today:

    card(facts, *, tier, checks=None, motion=None, look=None) -> str
    diff(prev, cur, *, checks_prev=None, checks_cur=None) -> str
    stub(k, summary) -> str
    subjects(facts, roles=None) -> list[str]

`facts` is an astra.facts/1 dict as astra_scene_facts returns it:
{camera, color, world, lights, floor, objects [{name, root, role?, ...}],
unseen, contacts, duplicates, composition, meter, map?, timeline, dropped,
budget}. Every function tolerates missing keys.
"""

import json

from . import scripts

# Character budgets per tier for the placeholder card. WS1's card is built to
# token budgets (standard card about 1.9k tokens; CHANGES 400 or fewer).
CARD_CHARS = {"small": 2400, "medium": 6000, "large": 12000}
DIFF_CHARS = 1600


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def card(facts, *, tier, checks=None, motion=None, look=None):
    """The evidence card for one facts snapshot, sized for the model's tier.

    checks: a QAReport (or its dict) whose failing checks are listed first;
    motion: an astra.motion/1 dict; look: an astra_look_check result.
    Placeholder: compact JSON of the inputs, head-and-tail capped per tier.
    """
    if tier not in CARD_CHARS:
        raise ValueError(f"Unknown tier {tier!r}")
    payload = {"facts": facts}
    if checks is not None:
        payload["checks"] = checks.to_dict() if hasattr(checks, "to_dict") else checks
    if motion is not None:
        payload["motion"] = motion
    if look is not None:
        payload["look"] = look
    return "SCENE FACTS\n" + scripts.cap(_compact(payload), CARD_CHARS[tier])


def _objects(facts):
    return {entry.get("name"): entry for entry in (facts or {}).get("objects", []) if isinstance(entry, dict)}


def _check_ids(checks):
    if checks is None:
        return set()
    if hasattr(checks, "failing"):
        checks = checks.failing
    elif isinstance(checks, dict):
        checks = checks.get("failing", checks.get("checks", []))
    ids = set()
    for item in checks:
        ids.add(item.get("id") if isinstance(item, dict) else str(item))
    return ids


def diff(prev, cur, *, checks_prev=None, checks_cur=None):
    """What changed between two facts snapshots, for the model after an edit.

    Placeholder: object names added, removed and changed (any field), and
    failing check ids that were resolved or are new. WS1 reports the
    measured deltas (moved 0.4 m, now resting, coverage 12% -> 31%).
    """
    before, after = _objects(prev), _objects(cur)
    added = [name for name in after if name not in before]
    removed = [name for name in before if name not in after]
    changed = [name for name in after if name in before and _compact(after[name]) != _compact(before[name])]
    lines = ["CHANGES"]
    if added:
        lines.append("added: " + ", ".join(map(str, added)))
    if removed:
        lines.append("removed: " + ", ".join(map(str, removed)))
    if changed:
        lines.append("changed: " + ", ".join(map(str, changed)))
    old_ids, new_ids = _check_ids(checks_prev), _check_ids(checks_cur)
    if old_ids - new_ids:
        lines.append("resolved checks: " + ", ".join(sorted(old_ids - new_ids)))
    if new_ids - old_ids:
        lines.append("new failing checks: " + ", ".join(sorted(new_ids - old_ids)))
    if len(lines) == 1:
        lines.append("no measured change")
    return scripts.cap("\n".join(lines), DIFF_CHARS)


def stub(k, summary):
    """The one line that replaces superseded evidence #k in the conversation."""
    return f"[evidence #{k} superseded: {summary}]"


def subjects(facts, roles=None):
    """The names the scene is about, grouped by assembly root.

    With roles (spec roles such as 'subject'), objects carrying one of them;
    otherwise every object that is not the floor. An 11-part car is one
    subject named after its root ('Car'), not 'CarBody'.
    """
    facts = facts or {}
    wanted = set(roles) if roles else None
    floor = (facts.get("floor") or {}).get("name")
    names = []
    for entry in facts.get("objects", []):
        if not isinstance(entry, dict) or entry.get("name") == floor:
            continue
        if wanted is not None and entry.get("role") not in wanted:
            continue
        name = entry.get("root") or entry.get("name")
        if name and name not in names:
            names.append(name)
    return names

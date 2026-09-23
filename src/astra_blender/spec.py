"""The checkable scene spec (astra.spec/1). Owned by WS4.

INTERFACE PINNED BY THE FOUNDATION. WS6 posts spec.draft(brief) at the start
of a run and hands astra_set_spec results to qa.evaluate; WS4 replaces the
bodies with category tables (English and Spanish keywords) and the full
astra.spec/1 validation:

    draft(brief) -> dict
    validate(spec) -> list[{path, message}]   ([] when valid)
    normalize(spec) -> dict

astra.spec/1: {schema, title, mood?, monochrome?, palette?, objects [{id,
names (globs), category?, role, count?, dims?, size?, material?, relations?,
smooth?, intentional?}], camera?, lighting?, motion?, deliverables?}.
"""

import copy

SCHEMA_ID = "astra.spec/1"
ROLES = ("subject", "support", "prop", "set", "background")


def draft(brief):
    """A starting spec for a brief. Placeholder: title only, no objects yet."""
    text = " ".join(str(brief or "").split())
    return {"schema": SCHEMA_ID, "title": text[:80] or "Untitled scene", "objects": []}


def validate(spec):
    """Structural problems of a spec as [{path, message}]; [] when valid.

    Placeholder: checks the shape WS4 relies on (title, objects with id,
    names and role). WS4 validates the whole schema and its units.
    """
    errors = []
    if not isinstance(spec, dict):
        return [{"path": "", "message": "a spec is a JSON object"}]
    if not isinstance(spec.get("title"), str) or not spec["title"].strip():
        errors.append({"path": "title", "message": "give the scene a title"})
    objects = spec.get("objects", [])
    if not isinstance(objects, list):
        return errors + [{"path": "objects", "message": "objects is a list"}]
    seen = set()
    for index, entry in enumerate(objects):
        where = f"objects[{index}]"
        if not isinstance(entry, dict):
            errors.append({"path": where, "message": "each object is a JSON object"})
            continue
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            errors.append({"path": where + ".id", "message": "each object needs an id"})
        elif entry["id"] in seen:
            errors.append({"path": where + ".id", "message": f"duplicate id {entry['id']!r}"})
        else:
            seen.add(entry["id"])
        names = entry.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            errors.append({"path": where + ".names", "message": "names is a non-empty list of name globs"})
        if entry.get("role") not in ROLES:
            errors.append({"path": where + ".role", "message": "role is one of " + ", ".join(ROLES)})
    return errors


def normalize(spec):
    """A copy with defaults filled in (schema id, count [1, 1])."""
    result = copy.deepcopy(spec)
    result.setdefault("schema", SCHEMA_ID)
    for entry in result.get("objects", []):
        if isinstance(entry, dict):
            entry.setdefault("count", [1, 1])
    return result

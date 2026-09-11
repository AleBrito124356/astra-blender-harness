"""Remember provider setups between sessions without writing secrets to disk.

A profile holds only non-secret fields (provider, model ID, API base, defaults)
in a JSON file under the user's config directory. The API key, when the user
opts in, goes to the operating system keyring instead: Credential Manager on
Windows, Keychain on macOS, Secret Service on Linux. If `keyring` is not
installed the profile still saves and the key simply is not remembered; Astra
never falls back to writing the secret in plain text.

No function here returns a stored secret to the browser. `load_secret` exists
for the run endpoint, which injects the key server-side so it never travels
back through the UI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SERVICE = "astra-blender-harness"
NAME_PATTERN = re.compile(r"^[\w .\-]{1,40}$")
MAX_PROFILES = 40


class Profile(BaseModel):
    """The non-secret half of a provider setup."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    provider: str = Field(default="custom", max_length=40)
    model: str = Field(min_length=1, max_length=200)
    api_base: str | None = Field(default=None, max_length=300)
    tool_mode: str = Field(default="native", max_length=10)
    vision: bool = True


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "astra-blender"


def _path() -> Path:
    return config_dir() / "profiles.json"


def _keyring():
    """Return the keyring module, or None when it is absent or has no backend."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailBackend
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), FailBackend):
            return None
    except Exception:
        return None
    return keyring


def secret_backend() -> str | None:
    """A short backend name for the UI, or None when secrets cannot be stored."""
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return type(keyring.get_keyring()).__name__
    except Exception:
        return None


def _read() -> dict[str, Profile]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = {}
    for entry in raw.get("profiles", []):
        try:
            profile = Profile.model_validate(entry)
        except ValueError:
            continue  # Skip an entry a newer or hand-edited file made invalid.
        found[profile.name] = profile
    return found


def _write(profiles: dict[str, Profile]) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"version": 1, "profiles": [p.model_dump() for p in profiles.values()]}, indent=2)
    # Write through a temporary file so an interrupted save cannot truncate the
    # existing profiles, and keep the file readable only by this user.
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".profiles-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, _path())
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def listing() -> list[dict]:
    """Profiles plus whether each one has a remembered key. Never includes secrets."""
    keyring = _keyring()
    result = []
    for profile in _read().values():
        stored = False
        if keyring is not None:
            try:
                stored = keyring.get_password(SERVICE, profile.name) is not None
            except Exception:
                stored = False
        result.append({**profile.model_dump(), "has_secret": stored})
    return sorted(result, key=lambda p: p["name"].lower())


def save(profile: Profile, api_key: str | None) -> dict:
    """Store a profile. A non-empty api_key is written to the keyring only."""
    if not NAME_PATTERN.match(profile.name):
        raise ValueError("Use 1-40 characters: letters, numbers, spaces, dots or dashes")
    profiles = _read()
    if profile.name not in profiles and len(profiles) >= MAX_PROFILES:
        raise ValueError(f"Profile limit reached ({MAX_PROFILES}). Delete one first.")
    profiles[profile.name] = profile
    _write(profiles)
    remembered = False
    if api_key:
        keyring = _keyring()
        if keyring is None:
            raise ValueError(
                "Profile saved, but the key was not: no OS keyring available. "
                "Install it with: pip install 'astra-blender-harness[keyring]'"
            )
        keyring.set_password(SERVICE, profile.name, api_key)
        remembered = True
    return {"saved": profile.name, "remembered": remembered}


def forget_secret(name: str) -> None:
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, name)
    except Exception:
        pass  # Nothing stored under this name, which is the state we wanted.


def delete(name: str) -> None:
    profiles = _read()
    if name in profiles:
        del profiles[name]
        _write(profiles)
    forget_secret(name)


def load_secret(name: str) -> str | None:
    """Server-side only: resolve a remembered key for a run."""
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except Exception:
        return None

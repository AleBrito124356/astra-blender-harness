from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def validate_api_base(value):
    """Reject credentialed, non-loopback-plaintext or query-bearing API bases."""
    if not value:
        return None
    url = urlparse(value)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError("Use an API base URL without credentials, query or fragment")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Remote API endpoints require HTTPS; HTTP is allowed only on loopback")
    return value.rstrip("/")


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["stdio", "http"] = "stdio"
    command: str = "uvx"
    args: list[str] = Field(default_factory=lambda: ["blender-mcp==1.9.1"])
    env: dict[str, str] = Field(
        default_factory=lambda: {"BLENDER_MCP_SAFE_MODE": "1", "BLENDER_MCP_DISABLE_TELEMETRY": "1"}
    )
    url: str = "http://127.0.0.1:8000/mcp"
    headers: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "get_scene_info",
            "get_object_info",
            "get_viewport_screenshot",
            "execute_blender_code",
        ]
    )


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=5, max_length=16000)
    model: str = Field(default="openai/gpt-6-astra", min_length=1, max_length=200)
    api_key: SecretStr = SecretStr("")
    api_base: str | None = None
    # Name of a saved profile whose remembered key the server injects when
    # api_key is empty. The key itself is never sent from the browser.
    profile: str | None = Field(default=None, max_length=40)
    tool_mode: Literal["native", "json"] = "native"
    vision: bool = True
    auto_approve: bool = False
    quality: Literal["draft", "studio", "final"] = "studio"
    screenshot_max_size: int = Field(default=1400, ge=256, le=2048)
    # Budgets. Zero means no limit on that axis: the run then stops only when
    # the model finishes, another budget bites, or you press Stop.
    max_steps: int = Field(default=24, ge=0, le=2000)
    max_output_tokens: int = Field(default=4096, ge=512, le=16384)
    max_total_tokens: int = Field(default=150000, ge=0, le=50_000_000)
    timeout_seconds: int = Field(default=1200, ge=0, le=86400)
    # Per-phase caps, previously hidden constants. Build defaulted to half of
    # max_steps, which is what silently ended a run mid-scene.
    build_max_steps: int = Field(default=0, ge=0, le=2000)
    inspect_max_steps: int = Field(default=3, ge=1, le=200)
    # Continue a previous run: its conversation is reloaded and the scene it
    # already built is still in Blender.
    resume_from: str | None = Field(default=None, max_length=64, pattern=r"^[0-9a-f]{32}$")

    @field_validator("max_steps")
    @classmethod
    def sane_steps(cls, value):
        if 0 < value < 6:
            raise ValueError("Use 0 for no turn limit, or at least 6 turns")
        return value

    @field_validator("max_total_tokens")
    @classmethod
    def sane_tokens(cls, value):
        if 0 < value < 4096:
            raise ValueError("Use 0 for no token limit, or at least 4096 tokens")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def sane_timeout(cls, value):
        if 0 < value < 30:
            raise ValueError("Use 0 for no deadline, or at least 30 seconds")
        return value

    @field_validator("api_base")
    @classmethod
    def validate_base(cls, value):
        return validate_api_base(value)

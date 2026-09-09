from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


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
    max_steps: int = Field(default=24, ge=6, le=80)
    max_output_tokens: int = Field(default=4096, ge=512, le=16384)
    max_total_tokens: int = Field(default=150000, ge=4096, le=1000000)
    timeout_seconds: int = Field(default=1200, ge=30, le=3600)

    @field_validator("api_base")
    @classmethod
    def validate_base(cls, value):
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

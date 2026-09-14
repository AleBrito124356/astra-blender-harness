from astra_blender import cli
from astra_blender.config import MCPConfig


def test_uses_the_interpreters_blender_mcp_when_uvx_is_missing(tmp_path, monkeypatch, capsys):
    # The very first start on a machine without uv failed at spawn time and was
    # reported as "Cannot reach Blender"; blender-mcp was installed all along.
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    fake = scripts / "blender-mcp.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(cli.sys, "executable", str(scripts / "python.exe"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    resolved = cli.fallback_transport(MCPConfig())
    assert resolved.command == str(fake)
    assert resolved.args == []
    assert resolved.env == MCPConfig().env
    assert "uvx is not installed" in capsys.readouterr().out


def test_keeps_uvx_when_it_is_installed(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/tools/uvx.exe")
    assert cli.fallback_transport(MCPConfig()).command == "uvx"


def test_never_overrides_an_explicit_command_or_http(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    custom = MCPConfig(command=str(tmp_path / "my-mcp.exe"), args=["--flag"])
    assert cli.fallback_transport(custom) == custom
    remote = MCPConfig(transport="http", url="https://mcp.example/mcp")
    assert cli.fallback_transport(remote) == remote


def test_leaves_the_default_alone_without_a_local_blender_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.fallback_transport(MCPConfig()).command == "uvx"

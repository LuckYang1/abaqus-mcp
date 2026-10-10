import argparse
import json
import os
import platform
import shutil
import sys
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from .client import AbaqusBridgeClient, BridgeError

PLUGIN_FILE_NAME = "abaqus_mcp_plugin.py"
COMMANDS = ("abaqus-mcp-server", "abaqus-mcp-check", "abaqus-mcp-doctor", "abaqus-mcp-setup", "abaqus-mcp-stop")
DEFAULT_CHECK_CODE = "import sys\nresult = {'python': sys.version.split()[0], 'ok': True}"


def _utf8_output() -> None:
    # Windows 控制台默认使用 GBK，统一改为 UTF-8，避免输出中文或特殊字符时报错
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _print_json(label: str, payload: dict[str, Any]) -> None:
    print(f"{label}:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def plugin_directory() -> Path:
    configured = os.environ.get("ABAQUS_MCP_PLUGIN_DIR")
    return Path(configured) if configured else Path.home() / "abaqus_plugins"


def packaged_plugin() -> bytes:
    return resources.files("abaqus_mcp").joinpath("gui_plugin.py").read_bytes()


def _plugin_status() -> dict[str, Any]:
    target = plugin_directory() / PLUGIN_FILE_NAME
    installed = target.is_file()
    return {
        "directory": str(plugin_directory()),
        "path": str(target),
        "installed": installed,
        "matchesPackage": installed and target.read_bytes() == packaged_plugin(),
    }


def _static_diagnostics() -> dict[str, Any]:
    client = AbaqusBridgeClient.from_environment()
    return {
        "package": {"name": "abaqus-mcp", "version": metadata.version("abaqus-mcp")},
        "runtime": {"python": sys.version, "executable": sys.executable, "platform": platform.platform()},
        "entrypoints": {command: shutil.which(command) for command in COMMANDS},
        "plugin": _plugin_status(),
        "bridge": {"host": client.host, "port": client.port, "timeout": client.timeout},
    }


def _client(args: argparse.Namespace) -> AbaqusBridgeClient:
    return AbaqusBridgeClient(host=args.host, port=args.port, timeout=args.timeout)


def _fail(exc: BridgeError) -> None:
    print(f"FAILED [{exc.state}] {exc.error_type}: {exc}", file=sys.stderr)
    if exc.hint:
        print(exc.hint, file=sys.stderr)
    raise SystemExit(1) from exc


def _add_connection_arguments(parser: argparse.ArgumentParser, default_timeout: str) -> None:
    parser.add_argument("--host", default=os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ABAQUS_MCP_PORT", "48152")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("ABAQUS_MCP_TIMEOUT", default_timeout)))


def _check(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="abaqus-mcp-check",
                                     description="Ping the Abaqus/CAE bridge and run a short Python snippet in the kernel.")
    _add_connection_arguments(parser, "30")
    parser.add_argument("--code", default=DEFAULT_CHECK_CODE, help="Python code to execute in the Abaqus kernel.")
    args = parser.parse_args(argv)
    client = _client(args)
    try:
        ping = client.ping()
        execution = client.execute(args.code)
    except BridgeError as exc:
        _fail(exc)
    print("Abaqus MCP bridge is reachable.")
    _print_json("Ping", ping)
    _print_json("Execution", execution)
    if not execution.get("ok"):
        raise SystemExit(1)


def _doctor(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="abaqus-mcp-doctor",
                                     description="Print installation diagnostics and optionally verify the bridge.")
    _add_connection_arguments(parser, "30")
    parser.add_argument("--verify-connection", action="store_true", help="Also ping the running Abaqus/CAE bridge.")
    args = parser.parse_args(argv)
    _print_json("Diagnostics", _static_diagnostics())
    if not args.verify_connection:
        return
    try:
        ping = _client(args).ping()
    except BridgeError as exc:
        _fail(exc)
    _print_json("Connection", ping)


def _setup(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="abaqus-mcp-setup",
                                     description="Install the Abaqus/CAE plugin into ABAQUS_MCP_PLUGIN_DIR or ~/abaqus_plugins.")
    parser.parse_args(argv)
    target_dir = plugin_directory()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / PLUGIN_FILE_NAME
    content = packaged_plugin()
    if target.is_file() and target.read_bytes() == content:
        print(f"Plugin already up to date: {target}")
    else:
        target.write_bytes(content)
        print(f"Installed Abaqus/CAE plugin: {target}")
    print()
    print("Start or restart Abaqus/CAE, then choose Plug-ins > Abaqus MCP > Start MCP Bridge.")


def _stop(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="abaqus-mcp-stop", description="Ask the running Abaqus/CAE bridge to stop.")
    _add_connection_arguments(parser, "10")
    args = parser.parse_args(argv)
    try:
        response = _client(args).stop()
    except BridgeError as exc:
        _fail(exc)
    _print_json("Stop", response)


def check_main() -> None:
    _utf8_output()
    _check(sys.argv[1:])


def doctor_main() -> None:
    _utf8_output()
    _doctor(sys.argv[1:])


def setup_main() -> None:
    _utf8_output()
    _setup(sys.argv[1:])


def stop_main() -> None:
    _utf8_output()
    _stop(sys.argv[1:])

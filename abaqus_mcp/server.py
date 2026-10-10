import base64
import json
import math
import time
import uuid
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import __version__, kernel_code
from .client import AbaqusBridgeClient, BridgeError

INSTRUCTIONS = """You are controlling a live Abaqus/CAE session through a local MCP bridge.

Check the connection with ping or check_abaqus_connection before working. Point the session at a clean, explicit analysis folder with set_workdir before creating jobs. Build and verify models in small run_python chunks instead of one large script: a single expression returns its value, and statements return the value assigned to result. When Abaqus API behaviour is uncertain, inspect the live objects with run_python before continuing. Prefer named sets and named surfaces over raw-coordinate selections for loads, boundary conditions, section assignments and interactions.

Every bridge call carries an executionId; correlate results with it. A call that timed out while it was running has an unknown outcome: inspect the model state before repeating a modelling command. Use get_model_info, list_jobs, monitor_job_status and inspect_odb to record solver and result evidence, and capture_viewport to look at the model. submit_job waits for the solver while CAE stays responsive; pass since from its result to monitor_job_status when following a run."""

PING_TIMEOUT = 10.0
SUBMIT_WAIT_TIMEOUT = 3600.0
SUBMIT_POLL_SECONDS = 3.0
TERMINATE_COMMAND_TIMEOUT = 60.0
MODEL_INFO_LIMIT = 200
TIMEOUT_MESSAGE = "timeout must be a finite positive number of seconds"
TIMEOUT_NOTE = " timeout (seconds) defaults to ABAQUS_MCP_TIMEOUT; the client's own tool timeout must be longer."
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

mcp = MCPServer("abaqus-mcp", version=__version__, instructions=INSTRUCTIONS)


def _result(data: dict[str, Any], *, error: bool = False, image: tuple[str, str] | None = None) -> CallToolResult:
    content: list[Any] = [TextContent(text=json.dumps(data, ensure_ascii=False, default=str))]
    if image is not None:
        content.append(ImageContent(data=image[0], mime_type=image[1]))
    return CallToolResult(content=content, structured_content=data, is_error=error)


def _text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=text)])


def _bridge_error(exc: BridgeError) -> dict[str, Any]:
    error: dict[str, Any] = {"type": exc.error_type, "message": str(exc)}
    if exc.hint is not None:
        error["hint"] = exc.hint
    return {"ok": False, "executionId": exc.execution_id, "state": exc.state, "error": error}


def _validation_error(message: str) -> CallToolResult:
    return _result({"ok": False, "executionId": str(uuid.uuid4()), "error": {"type": "ValueError", "message": message}},
                   error=True)


def _invalid_timeout(timeout: float | None) -> bool:
    return timeout is not None and not (math.isfinite(timeout) and timeout > 0)


async def _call(method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
    client = AbaqusBridgeClient.from_environment()
    return await anyio.to_thread.run_sync(lambda: client.request(method, params, timeout))


async def _execute(code: str, timeout: float | None = None) -> dict[str, Any]:
    return await _call("execute", {"code": code}, timeout)


def _python_error(execution: dict[str, Any]) -> dict[str, Any]:
    error_type = execution.get("error_type", "ExecutionError").rsplit(".", 1)[-1]
    error: dict[str, Any] = {
        "type": error_type,
        "message": execution.get("message") or execution.get("core_error", "Abaqus execution failed"),
    }
    primary = execution.get("primary_frame") or {}
    code_line = execution.get("code_line")
    if code_line is None:
        excerpt = execution.get("code_excerpt") or ""
        marked = next((line for line in excerpt.splitlines() if line.startswith(">>")), "")
        code_line = marked.split("|", 1)[-1].strip() if marked else None
    location = {"file": primary.get("file"), "line": primary.get("line") or execution.get("error_line"),
                "function": primary.get("function"), "code": code_line}
    location = {key: value for key, value in location.items() if value is not None}
    if location:
        error["at"] = location
    recovery = execution.get("recovery") or {}
    kept = ("missing_key", "possible_keys", "available_key_count", "available_keys_sample",
            "missing_attribute", "object_type", "possible_members", "missing_variable",
            "possible_names", "import_suggestion", "call_target", "callable_signature",
            "possible_keywords", "syntax_offset")
    suggestions = {key: recovery[key] for key in kept
                   if key in recovery and recovery[key] is not None and recovery[key] != []}
    if suggestions:
        error["suggestions"] = suggestions
    user_frames = [frame for frame in execution.get("frames", [])
                   if frame.get("file", "").startswith("<abaqus-mcp:")
                   or frame.get("file") == primary.get("file")]
    if user_frames and primary and user_frames[-1] == primary:
        user_frames.pop()
    if user_frames:
        error["callers"] = user_frames[-3:]
    return error


def _execution_result(execution: dict[str, Any]) -> CallToolResult:
    data: dict[str, Any] = {"executionId": execution["executionId"], "ok": bool(execution.get("ok"))}
    if data["ok"]:
        data["value"] = execution.get("return_value")
    else:
        data["error"] = _python_error(execution)
    for stream in ("stdout", "stderr"):
        if execution.get(stream):
            data[stream] = execution[stream]
    return _result(data, error=not data["ok"])


def _operation_data(execution: dict[str, Any]) -> dict[str, Any]:
    value = execution["return_value"]
    if not isinstance(value, dict):
        raise TypeError("Abaqus operation did not return an object")
    data = {"ok": True, "executionId": execution["executionId"], **value}
    for stream in ("stdout", "stderr"):
        if execution.get(stream):
            data[stream] = execution[stream]
    return data


async def _operation(code: str, timeout: float | None) -> CallToolResult:
    try:
        execution = await _execute(code, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not execution.get("ok"):
        return _execution_result(execution)
    return _result(_operation_data(execution))


def _jobs_code(job_name: str, terminate: bool, since: float | None) -> str:
    return kernel_code.render(kernel_code.JOBS, job_name=job_name, terminate=terminate, since=since,
                              terminate_timeout=TERMINATE_COMMAND_TIMEOUT)


def _odb_code(odb_path: str, step: str = "", frame: int = -1, variable: str = "", set_name: str = "",
              component: str = "", history_region: str = "", history_variable: str = "", max_points: int = 200) -> str:
    return kernel_code.render(kernel_code.ODB, odb_path=odb_path, step=step, frame=frame, variable=variable,
                              set_name=set_name, component=component, history_region=history_region,
                              history_variable=history_variable, max_points=max_points)


async def _capture(viewport_name: str, image_format: str, save_path: str,
                   timeout: float | None) -> tuple[dict[str, Any] | None, str | None, CallToolResult | None]:
    code = kernel_code.render(kernel_code.CAPTURE, viewport_name=viewport_name, image_format=image_format,
                              save_path=save_path)
    try:
        execution = await _execute(code, timeout)
    except BridgeError as exc:
        return None, None, _result(_bridge_error(exc), error=True)
    if not execution.get("ok"):
        return None, None, _execution_result(execution)
    data = _operation_data(execution)
    encoded_image = data.pop("imageBase64")
    if encoded_image is not None:
        try:
            base64.b64decode(encoded_image, validate=True)
        except ValueError as exc:
            return None, None, _result({"ok": False, "executionId": data["executionId"],
                                        "error": {"type": "ProtocolError", "message": str(exc)}}, error=True)
    return data, encoded_image, None


@mcp.tool(
    description="Check the active Abaqus/CAE connection and return session and bridge details. timeout (seconds) defaults to 10.",
    annotations=READ_ONLY,
)
async def ping(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        session = await _call("ping", timeout=PING_TIMEOUT if timeout is None else timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    return _result({"ok": True, "executionId": session["executionId"], "session": session})


@mcp.tool(
    description="Return a short human-readable status of the Abaqus/CAE bridge connection. timeout (seconds) defaults to 10.",
    annotations=READ_ONLY,
)
async def check_abaqus_connection(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        session = await _call("ping", timeout=PING_TIMEOUT if timeout is None else timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    bridge = session["bridge"]
    return _text_result(
        f"Connected to Abaqus/CAE {session['abaqus_version']} through the MCP bridge at "
        f"{bridge['host']}:{bridge['port']} (bridge {bridge['version']}).\n"
        f"Models: {session['models']}\n"
        f"Viewports: {session['viewports']}\n"
        f"Working directory: {session['cwd']}"
    )


@mcp.tool(
    description="Run Python in the active Abaqus/CAE kernel. A single expression returns its value; statements return "
                "the value assigned to result. Variables persist between calls." + TIMEOUT_NOTE,
)
async def run_python(code: str, timeout: float | None = None) -> CallToolResult:
    if not code.strip():
        return _validation_error("code must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        execution = await _execute(code, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    return _execution_result(execution)


@mcp.tool(
    description="Compatibility form of run_python that takes script and returns plain text: captured stdout followed "
                "by the returned value as JSON." + TIMEOUT_NOTE,
)
async def execute_script(script: str, timeout: float | None = None) -> CallToolResult:
    if not script.strip():
        return _validation_error("script must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        execution = await _execute(script, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not execution.get("ok"):
        return _execution_result(execution)
    stdout = execution.get("stdout") or ""
    value = execution.get("return_value")
    if value is not None:
        text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        return _text_result(stdout + "\n" + text if stdout else text)
    return _text_result(stdout if stdout else "(Script executed successfully, no output)")


@mcp.tool(
    description="Set the Abaqus/CAE working directory to an existing absolute path and return the previous and "
                "current paths." + TIMEOUT_NOTE,
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def set_workdir(path: str, timeout: float | None = None) -> CallToolResult:
    if not path.strip():
        return _validation_error("path must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(kernel_code.render(kernel_code.SET_WORKDIR, path=path.strip()), timeout)


@mcp.tool(
    description="Summarize every model (parts, materials, sections, steps, loads, boundary conditions, interactions, "
                "constraints, amplitudes, instances, sets, surfaces and part topology), the jobs and the viewports. "
                f"Each name list holds at most {MODEL_INFO_LIMIT} entries; counts gives the full sizes." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_model_info(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(kernel_code.render(kernel_code.MODEL_INFO, limit=MODEL_INFO_LIMIT), timeout)


@mcp.tool(
    description="List the jobs registered in the current CAE session with their status and settings." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def list_jobs(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(_jobs_code("", False, None), timeout)


@mcp.tool(
    description="Submit an existing job of the current CAE session and wait until it finishes. The server polls the "
                "job with short calls, so CAE stays responsive and monitor_job_status keeps working. timeout (seconds, "
                "default 3600) bounds the wait; when it elapses first the job keeps running and the result carries "
                "since for monitor_job_status. The client's own tool timeout must be longer.",
)
async def submit_job(job_name: str, timeout: float | None = None) -> CallToolResult:
    name = job_name.strip()
    if not name:
        return _validation_error("job_name must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    wait_limit = SUBMIT_WAIT_TIMEOUT if timeout is None else timeout
    try:
        submission = await _execute(kernel_code.render(kernel_code.SUBMIT_JOB, job_name=name))
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not submission.get("ok"):
        return _execution_result(submission)
    submitted = submission["return_value"]
    started = time.monotonic()
    while True:
        await anyio.sleep(min(SUBMIT_POLL_SECONDS, max(0.0, wait_limit - (time.monotonic() - started))))
        try:
            execution = await _execute(_jobs_code(name, False, submitted["submittedAt"]))
        except BridgeError as exc:
            return _result({**_bridge_error(exc), "submission": submitted}, error=True)
        if not execution.get("ok"):
            return _result({"ok": False, "executionId": execution["executionId"], "error": _python_error(execution),
                            "submission": submitted}, error=True)
        status = execution["return_value"]
        if status["terminal"] or time.monotonic() - started >= wait_limit:
            break
    data: dict[str, Any] = {
        "ok": True,
        "executionId": submission["executionId"],
        "jobName": name,
        "submittedAt": submitted["submittedAt"],
        "workingDirectory": submitted["workingDirectory"],
        "waitedSeconds": round(time.monotonic() - started, 1),
        "completed": status["terminal"],
        "state": status["state"],
        "status": status,
        "statusExecutionId": execution["executionId"],
    }
    if submission.get("stdout"):
        data["submitStdout"] = submission["stdout"]
    if not status["terminal"]:
        data["note"] = "The job is still running. Follow it with monitor_job_status using job_name and since=submittedAt."
    return _result(data)


@mcp.tool(
    description="Without job_name, list the jobs. With job_name, combine the CAE status with the .log/.sta/.dat/.msg "
                "files to report progress, terminal state, diagnostics and evidence. since (Unix seconds) binds "
                "file-only evidence to one run. terminate=true sends the Abaqus terminate command for a job of the "
                "current CAE session; confirm the TERMINATED state with a later call." + TIMEOUT_NOTE,
)
async def monitor_job_status(job_name: str = "", terminate: bool = False, since: float | None = None,
                             timeout: float | None = None) -> CallToolResult:
    if terminate and not job_name.strip():
        return _validation_error("job_name is required for termination")
    if since is not None and not 0 <= since <= 4102444800:
        return _validation_error("since must be a Unix timestamp between 1970 and 2100")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(_jobs_code(job_name.strip(), terminate, since), timeout)


@mcp.tool(
    description="Inspect an ODB read-only: steps (start, end and duration times), sampled frames, field outputs with "
                "components, invariants and positions, history regions and sets. Optionally summarize one field "
                "(min/max) at step/frame/set/component, or return a sampled history curve by region and variable. "
                "Vector fields without component use the magnitude; tensor fields need a component." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def inspect_odb(odb_path: str, step: str = "", frame: int = -1, variable: str = "", set_name: str = "",
                      component: str = "", history_region: str = "", history_variable: str = "",
                      max_points: int = 200, timeout: float | None = None) -> CallToolResult:
    if not odb_path.strip():
        return _validation_error("odb_path must not be empty")
    if not 2 <= max_points <= 1000:
        return _validation_error("max_points must be between 2 and 1000")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(_odb_code(odb_path.strip(), step, frame, variable, set_name, component, history_region,
                                      history_variable, max_points), timeout)


@mcp.tool(
    description="Compatibility form of inspect_odb that returns the ODB metadata only." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_odb_info(odb_path: str, timeout: float | None = None) -> CallToolResult:
    if not odb_path.strip():
        return _validation_error("odb_path must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(_odb_code(odb_path.strip()), timeout)


@mcp.tool(
    description="Capture a CAE viewport. PNG and TIFF return a native MCP image and can also be saved; SVG, EPS and PS "
                "require save_path. The structured result never contains the image base64." + TIMEOUT_NOTE,
)
async def capture_viewport(viewport_name: str = "", image_format: str = "PNG", save_path: str = "",
                           timeout: float | None = None) -> CallToolResult:
    image_format = image_format.upper()
    if image_format not in ("PNG", "TIFF", "SVG", "EPS", "PS"):
        return _validation_error("image_format must be PNG, TIFF, SVG, EPS or PS")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    data, encoded_image, failure = await _capture(viewport_name, image_format, save_path, timeout)
    if failure is not None:
        return failure
    if encoded_image is None:
        return _result(data)
    return _result(data, image=(encoded_image, "image/png" if image_format == "PNG" else "image/tiff"))


@mcp.tool(
    description="Compatibility form of capture_viewport that returns the PNG or TIFF image as a data URI text."
                + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_viewport_image(viewport_name: str = "", image_format: str = "PNG",
                             timeout: float | None = None) -> CallToolResult:
    image_format = image_format.upper()
    if image_format not in ("PNG", "TIFF"):
        return _validation_error("image_format must be PNG or TIFF")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    data, encoded_image, failure = await _capture(viewport_name, image_format, "", timeout)
    if failure is not None:
        return failure
    return _text_result(f"data:image/{data['format']};base64,{encoded_image}")


async def _telemetry() -> str:
    try:
        session = await _call("ping", timeout=5.0)
    except BridgeError as exc:
        return json.dumps({"connected": False, **_bridge_error(exc)}, ensure_ascii=False)
    return json.dumps({"connected": True, "session": session}, ensure_ascii=False)


@mcp.resource("abaqus://session-telemetry", name="session-telemetry",
              description="Live Abaqus/CAE session and bridge telemetry.", mime_type="application/json")
async def session_telemetry() -> str:
    return await _telemetry()


@mcp.resource("abaqus://status", name="status",
              description="The same telemetry as abaqus://session-telemetry, for clients that read abaqus://status.",
              mime_type="application/json")
async def status_resource() -> str:
    return await _telemetry()


@mcp.resource("abaqus://agent-instructions", name="agent-instructions",
              description="Working rules for agents that control Abaqus/CAE through this server.",
              mime_type="text/plain")
def agent_instructions() -> str:
    return INSTRUCTIONS


def main() -> None:
    mcp.run(transport="stdio")

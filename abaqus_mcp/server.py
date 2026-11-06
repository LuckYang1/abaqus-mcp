import base64
import json
import math
import os
import re
import time
import uuid
from typing import Any

import anyio
import psutil
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import __version__, kernel_code
from .client import AbaqusBridgeClient, BridgeError

INSTRUCTIONS = """You are controlling a live Abaqus/CAE session through a local MCP bridge.

Check the connection with ping or check_abaqus_connection before working. Point the session at a clean, explicit analysis folder with set_workdir before creating jobs; Abaqus/CAE keeps its working directory open, so that folder cannot be deleted while CAE runs. Build and verify models in small run_python chunks instead of one large script: a single expression returns its value, and statements return the value assigned to result. Abaqus constants, numpy values and tuple keys in the result are converted to JSON automatically; objects that cannot be converted are listed in replaced. Run project script files with run_script_file instead of exec(open(...).read()).

Each conversation has its own run_python namespace, preloaded like the Abaqus/CAE command line (from abaqus import *, from abaqusConstants import *, from caeModules import *, regionToolset, odbAccess), except that sum and pow stay Python's built-ins (the Abaqus XY-data versions are abaqus.sum and abaqus.pow). Variables persist between your calls; reset_namespace restores the initial state. Do not assign to Abaqus module names such as part, mesh, step, load or job; import a module under another name when you need it. When you record an Abaqus exception, use abaqus_error_text(e) or str(e), never repr(e) or e.args.

When Abaqus API behaviour is uncertain, inspect the live objects with run_python before continuing; api_help shows __doc__ (outdated for some Abaqus 2024 methods) together with real calls recorded in abaqus.rpy. Prefer named sets and named surfaces over raw-coordinate selections for loads, boundary conditions, section assignments and interactions. get_step_states shows how loads, boundary conditions, interactions, predefined fields and output requests change from step to step; write_input shows the keywords Abaqus/CAE writes; find_keyword_blocks and edit_keyword_block inspect and change the keyword block.

Every bridge call carries an executionId; correlate results with it. While the kernel runs a call, Abaqus/CAE cannot answer any request, so a call whose timeout elapses while its code is still running returns state UNKNOWN (or RUNNING): follow it with get_execution, whose answer arrives once the kernel has finished, instead of repeating a modelling command; give long modelling scripts a larger timeout. Use get_model_info, list_jobs, monitor_job_status and inspect_odb to record solver and result evidence, and capture_viewport to look at the model. submit_job waits for the solver while CAE stays responsive; for long jobs pass wait=false and follow the run with monitor_job_status(job_name, since, wait_seconds, until). Close ODBs you opened in run_python with try/finally; list_open_odbs and close_odbs manage the ODBs open in the session."""

PING_TIMEOUT = 10.0
SUBMIT_WAIT_TIMEOUT = 3600.0
SUBMIT_POLL_SECONDS = 3.0
SUBMIT_START_SECONDS = 30.0
TERMINATE_COMMAND_TIMEOUT = 60.0
MODEL_INFO_LIMIT = 200
GROUP_LIMIT = 20
MAX_WAIT_SECONDS = 86400.0
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "120"))
TIMEOUT_MESSAGE = "timeout must be a finite positive number of seconds"
TIMEOUT_NOTE = (f" timeout (seconds) defaults to ABAQUS_MCP_TIMEOUT, currently {DEFAULT_TIMEOUT:g} s; pass a larger "
                "value for long modelling scripts. The client's own tool timeout must be longer.")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
STEP_STATE_KINDS = ["loads", "boundary_conditions", "interactions", "predefined_fields", "field_outputs",
                    "history_outputs"]
SOLVER_PROCESSES = {"standard.exe", "explicit.exe", "explicit_dp.exe", "pre.exe", "package.exe", "smalauncher.exe"}
UNTIL_PATTERN = re.compile(r"^(terminal|changed|step>=(\d+))$")
TERMINAL_STATES = ("COMPLETED", "FAILED", "TERMINATED", "DEAD")
# 每个对话由客户端启动一个服务端进程；命名空间按服务端进程区分，各对话互不影响
SESSION_NAMESPACE = f"conversation-{uuid.uuid4().hex[:12]}"

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


def _invalid_wait(wait_seconds: float) -> bool:
    return not (math.isfinite(wait_seconds) and 0 <= wait_seconds <= MAX_WAIT_SECONDS)


def _client() -> AbaqusBridgeClient:
    return AbaqusBridgeClient.from_environment()


async def _execute(code: str, timeout: float | None = None, namespace: str | None = None) -> dict[str, Any]:
    client = _client()
    return await anyio.to_thread.run_sync(lambda: client.execute(code, timeout, namespace))


async def _run(request: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    client = _client()
    return await anyio.to_thread.run_sync(lambda: client.run(request, timeout))


def _python_error(execution: dict[str, Any]) -> dict[str, Any]:
    error_type = execution.get("error_type", "ExecutionError").rsplit(".", 1)[-1]
    error: dict[str, Any] = {
        "type": error_type,
        "message": execution.get("message") or execution.get("core_error", "Abaqus execution failed"),
    }
    if execution.get("raw_message"):
        error["raw_message"] = execution["raw_message"]
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
            "possible_names", "import_suggestion", "call_target", "callable_signature", "signature_source",
            "invalid_keyword", "possible_keywords", "syntax_offset")
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


def _execution_data(execution: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {"executionId": execution["executionId"], "ok": bool(execution.get("ok"))}
    if data["ok"]:
        data["value"] = execution.get("return_value")
        if execution.get("replaced"):
            data["replaced"] = execution["replaced"]
    else:
        data["error"] = _python_error(execution)
    for key in ("stdout", "stderr", "warnings"):
        if execution.get(key):
            data[key] = execution[key]
    # 只在命名空间是本次新建时返回，提示以前的变量已不存在
    if (execution.get("namespace") or {}).get("created"):
        data["namespace"] = execution["namespace"]
    return data


def _execution_result(execution: dict[str, Any]) -> CallToolResult:
    data = _execution_data(execution)
    return _result(data, error=not data["ok"])


def _operation_data(execution: dict[str, Any]) -> dict[str, Any]:
    value = execution["return_value"]
    if not isinstance(value, dict):
        raise TypeError("Abaqus operation did not return an object")
    data = {"ok": True, "executionId": execution["executionId"], **value}
    for stream in ("stdout", "stderr"):
        if execution.get(stream):
            data[stream] = execution[stream]
    if execution.get("replaced"):
        data["replaced"] = execution["replaced"]
    return data


async def _operation(code: str, timeout: float | None) -> CallToolResult:
    try:
        execution = await _execute(code, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not execution.get("ok"):
        return _execution_result(execution)
    return _result(_operation_data(execution))


def _solver_processes(job_name: str) -> list[dict[str, Any]] | None:
    # 求解进程只能在本机查看；桥接不在本机时返回 None，作业状态只按文件判断
    if not job_name or not _client().is_local:
        return None
    wanted = job_name.lower()
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "cwd", "create_time"], ad_value=None):
        name = (process.info["name"] or "").lower()
        if name not in SOLVER_PROCESSES:
            continue
        raw = process.info["cmdline"] or []
        args = [argument.lower() for argument in raw]
        by_flag = any(argument == "-job" and index + 1 < len(args) and args[index + 1] == wanted
                      for index, argument in enumerate(args))
        if not (by_flag or f"job={wanted}" in args):
            continue
        # 求解器在临时目录中运行，作业目录写在 -indir/-outdir；启动程序在作业目录中运行
        directories = [raw[index + 1] for index, argument in enumerate(args)
                       if argument in ("-indir", "-outdir") and index + 1 < len(raw)]
        if name == "smalauncher.exe" and process.info["cwd"]:
            directories.append(process.info["cwd"])
        directories = list(dict.fromkeys(directories))
        found.append({"pid": process.info["pid"], "name": process.info["name"], "directories": directories,
                      "startedAt": process.info["create_time"], "commandLine": " ".join(raw)[:500]})
    return found


def _jobs_code(job_name: str, terminate: bool, since: float | None, directory: str = "",
               diagnostics: bool = True, warning_detail: str = "") -> str:
    return kernel_code.render(kernel_code.JOBS, job_name=job_name, terminate=terminate, since=since,
                              terminate_timeout=TERMINATE_COMMAND_TIMEOUT, directory=directory,
                              processes=_solver_processes(job_name), diagnostics=diagnostics,
                              warning_detail=warning_detail, group_limit=GROUP_LIMIT)


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
    description="Check the active Abaqus/CAE connection and return session and bridge details, including the startup "
                "directory of Abaqus/CAE (bridge.gui_cwd, where abaqus.rpy is written) and the size of this "
                "conversation's run_python namespace. timeout (seconds) defaults to 10.",
    annotations=READ_ONLY,
)
async def ping(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    client = _client()
    try:
        session = await anyio.to_thread.run_sync(
            lambda: client.ping(PING_TIMEOUT if timeout is None else timeout, SESSION_NAMESPACE))
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
    client = _client()
    try:
        session = await anyio.to_thread.run_sync(
            lambda: client.ping(PING_TIMEOUT if timeout is None else timeout, SESSION_NAMESPACE))
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
                "the value assigned to result. Abaqus constants, numpy values and non-string keys are converted to "
                "JSON; objects that cannot be converted become repr text and their paths are listed in replaced. "
                "Each conversation has its own namespace, preloaded like the CAE command line (abaqus, "
                "abaqusConstants, caeModules, regionToolset, odbAccess) plus abaqus_error_text(e), keeping Python's "
                "built-in sum and pow (the Abaqus XY-data versions are abaqus.sum and abaqus.pow); variables persist "
                "between calls. namespace selects another named namespace, for example one shared by several "
                "conversations. A timeout that elapses while the code is still running returns state UNKNOWN (or "
                "RUNNING); follow it with get_execution instead of running the code again." + TIMEOUT_NOTE,
)
async def run_python(code: str, namespace: str | None = None, timeout: float | None = None) -> CallToolResult:
    if not code.strip():
        return _validation_error("code must not be empty")
    if namespace is not None and not namespace.strip():
        return _validation_error("namespace must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        execution = await _execute(code, timeout, namespace or SESSION_NAMESPACE)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    return _execution_result(execution)


@mcp.tool(
    description="Compatibility form of run_python that takes script and returns plain text: captured stdout followed "
                "by the returned value as JSON. It uses the same namespace as run_python." + TIMEOUT_NOTE,
)
async def execute_script(script: str, namespace: str | None = None, timeout: float | None = None) -> CallToolResult:
    if not script.strip():
        return _validation_error("script must not be empty")
    if namespace is not None and not namespace.strip():
        return _validation_error("namespace must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        execution = await _execute(script, timeout, namespace or SESSION_NAMESPACE)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not execution.get("ok"):
        return _execution_result(execution)
    stdout = execution.get("stdout") or ""
    value = execution.get("return_value")
    if value is not None:
        text = json.dumps(value, indent=2, ensure_ascii=False)
        return _text_result(stdout + "\n" + text if stdout else text)
    return _text_result(stdout if stdout else "(Script executed successfully, no output)")


@mcp.tool(
    description="Run a Python script file in the Abaqus/CAE kernel like abaqus cae noGUI would: the file is read as "
                "UTF-8 and compiled with its real path (errors point at the file and line), __file__ and "
                "__name__ == '__main__' are set, sys.argv[1:] = args, the script directory is put on sys.path, and "
                "variables (a JSON object) are placed in the script globals. The value of result_variable is "
                "returned. fresh_modules lists directories: modules already imported from files inside them are "
                "removed from sys.modules first, so the script imports their current code. cwd changes the working "
                "directory only while the script runs. The script globals stay available as last_script_globals in "
                "the run_python namespace." + TIMEOUT_NOTE,
)
async def run_script_file(path: str, args: list[str] | None = None, variables: dict[str, Any] | None = None,
                          result_variable: str = "result", fresh_modules: list[str] | None = None, cwd: str = "",
                          namespace: str | None = None, timeout: float | None = None) -> CallToolResult:
    if not path.strip() or not os.path.isabs(path.strip()):
        return _validation_error("path must be an absolute path")
    if not result_variable.isidentifier():
        return _validation_error("result_variable must be a Python identifier")
    if cwd and not os.path.isabs(cwd):
        return _validation_error("cwd must be an absolute path")
    if any(not os.path.isabs(directory) for directory in fresh_modules or []):
        return _validation_error("fresh_modules must contain absolute directory paths")
    if namespace is not None and not namespace.strip():
        return _validation_error("namespace must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    request = {"mode": "script", "namespace": namespace or SESSION_NAMESPACE,
               "script": {"path": path.strip(), "args": list(args or []), "variables": dict(variables or {}),
                          "result_variable": result_variable, "fresh_modules": list(fresh_modules or []),
                          "cwd": cwd}}
    try:
        execution = await _run(request, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    data = _execution_data(execution)
    if data["ok"]:
        script = data.pop("value")
        data["value"] = script.pop("value")
        data["script"] = script
    return _result(data, error=not data["ok"])


@mcp.tool(
    description="Restore a run_python namespace to its initial preloaded state, dropping every variable. Without "
                "namespace it resets this conversation's namespace." + TIMEOUT_NOTE,
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
)
async def reset_namespace(namespace: str | None = None, timeout: float | None = None) -> CallToolResult:
    if namespace is not None and not namespace.strip():
        return _validation_error("namespace must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    try:
        execution = await _run({"mode": "reset", "namespace": namespace or SESSION_NAMESPACE}, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    return _execution_result(execution)


@mcp.tool(
    description="Follow a bridge call by its executionId, for example one that returned state UNKNOWN or RUNNING "
                "after its timeout. Returns QUEUED, RUNNING, COMPLETED, FAILED or CANCELLED with the submit, start and "
                "finish times; once a call whose caller stopped waiting has finished, result holds its value or error "
                "and can be read again later. Abaqus/CAE answers only after the kernel has finished the code it is "
                "running, so this call itself waits for that; wait_seconds additionally waits for a queued call. The "
                "bridge keeps the latest 200 calls and the results of the latest 20 calls whose caller stopped "
                "waiting since it started." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_execution(execution_id: str, wait_seconds: float = 0.0, timeout: float | None = None) -> CallToolResult:
    if not execution_id.strip():
        return _validation_error("execution_id must not be empty")
    if _invalid_wait(wait_seconds):
        return _validation_error(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS:g}")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    client = _client()
    try:
        summary = await anyio.to_thread.run_sync(lambda: client.execution(execution_id.strip(), wait_seconds, timeout))
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    data: dict[str, Any] = {"ok": True, "executionId": summary["executionId"], "target": execution_id.strip()}
    for key in ("state", "submittedAt", "startedAt", "finishedAt", "abandoned", "delivered", "note", "bridgeError"):
        if key in summary:
            data[key] = summary[key]
    if "result" in summary:
        data["result"] = _execution_data(summary["result"])
    return _result(data)


@mcp.tool(
    description="Set the Abaqus/CAE working directory to an existing absolute path and return the previous and "
                "current paths. Abaqus/CAE keeps its working directory open, so it cannot be deleted while CAE runs."
                + TIMEOUT_NOTE,
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
                f"Each name list holds at most {MODEL_INFO_LIMIT} entries; counts gives the full sizes. It lists "
                "names only; use get_step_states for step-by-step states and values." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_model_info(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(kernel_code.render(kernel_code.MODEL_INFO, limit=MODEL_INFO_LIMIT), timeout)


@mcp.tool(
    description="Show how loads, boundary conditions, interactions (including model changes), predefined fields and "
                "field/history output requests behave in each analysis step: creation step, region, and only the "
                "steps where their status or values change (status CREATED, MODIFIED, DEACTIVATED and the values "
                "whose ...State is SET). steps also summarises every analysis step (type, time period, increments, "
                "tolerances) with the objects created, modified and deactivated in it. kinds, names and steps "
                "(fnmatch patterns) narrow the output; max_entries bounds the number of objects." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def get_step_states(model: str, kinds: list[str] | None = None, names: list[str] | None = None,
                          steps: list[str] | None = None, max_entries: int = 200,
                          timeout: float | None = None) -> CallToolResult:
    if not model.strip():
        return _validation_error("model must not be empty")
    chosen = list(kinds) if kinds else STEP_STATE_KINDS
    unknown = [kind for kind in chosen if kind not in STEP_STATE_KINDS]
    if unknown:
        return _validation_error(f"unknown kinds {unknown}; choose from {STEP_STATE_KINDS}")
    if not 1 <= max_entries <= 2000:
        return _validation_error("max_entries must be between 1 and 2000")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.STEP_STATES, model=model, kinds=chosen, names=list(names or []),
                              steps=list(steps or []), max_entries=max_entries)
    return await _operation(code, timeout)


@mcp.tool(
    description="Write the input file of a model with a temporary job and show chosen keywords. The working "
                "directory is restored and the temporary job removed afterwards; the .inp file is kept unless "
                "keep_file=false. keywords (for example ['*Material', '*Initial Conditions']) and name_filter (a "
                "regular expression on the name= parameter) select keywords, parsed with the Abaqus inpParser module; "
                "each match gives its parameters, comments and up to max_data_lines data lines, and keywordCounts "
                "counts every keyword. inpParser reports keyword names in lower case without spaces and parameter "
                "values in upper case; read the file for the exact text. Without keywords and name_filter only the "
                "counts are returned. A restart model needs the base job's .odb, .res, .mdl, .stt and .prt files."
                + TIMEOUT_NOTE,
)
async def write_input(model: str, directory: str = "", job_name: str = "", keywords: list[str] | None = None,
                      name_filter: str = "", max_data_lines: int = 50, max_blocks: int = 20, keep_file: bool = True,
                      timeout: float | None = None) -> CallToolResult:
    if not model.strip():
        return _validation_error("model must not be empty")
    if directory and not os.path.isabs(directory):
        return _validation_error("directory must be an absolute path")
    if not 0 <= max_data_lines <= 10000:
        return _validation_error("max_data_lines must be between 0 and 10000")
    if not 1 <= max_blocks <= 500:
        return _validation_error("max_blocks must be between 1 and 500")
    if name_filter:
        try:
            re.compile(name_filter)
        except re.error as exc:
            return _validation_error(f"name_filter is not a valid regular expression: {exc}")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.WRITE_INPUT, model=model, directory=directory, job_name=job_name.strip(),
                              keywords=list(keywords or []), name_filter=name_filter, max_data_lines=max_data_lines,
                              max_blocks=max_blocks, keep_file=keep_file)
    return await _operation(code, timeout)


@mcp.tool(
    description="Search the keyword block of a model (synchronized with storeNodesAndElements=False) with a regular "
                "expression and return the matching blocks with their positions; context adds the first lines of "
                "neighbouring blocks." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def find_keyword_blocks(model: str, pattern: str, context: int = 0, max_blocks: int = 20,
                              max_chars: int = 4000, timeout: float | None = None) -> CallToolResult:
    if not model.strip():
        return _validation_error("model must not be empty")
    try:
        re.compile(pattern)
    except re.error as exc:
        return _validation_error(f"pattern is not a valid regular expression: {exc}")
    if not 0 <= context <= 10 or not 1 <= max_blocks <= 500 or not 100 <= max_chars <= 200000:
        return _validation_error("context must be 0-10, max_blocks 1-500 and max_chars 100-200000")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.FIND_KEYWORD_BLOCKS, model=model, pattern=pattern, context=context,
                              max_blocks=max_blocks, max_chars=max_chars)
    return await _operation(code, timeout)


@mcp.tool(
    description="Edit the keyword block of a model. action is insert_after, insert_before, replace or delete; anchor "
                "is a block position or a regular expression, and nothing changes unless it matches exactly "
                "expect_count blocks. Inserting text identical to an existing block is refused. Text with *Node or "
                "*Element data gets a warning: those data lines are dropped when the block is synchronized with "
                "storeNodesAndElements=False, so put them in a separate file and reference it with *Include. Copies "
                "made with mdb.Model(objectToCopy=...) merge a leading ** comment line into the previous block, so "
                "anchor on data lines rather than on comments. Check the result with write_input." + TIMEOUT_NOTE,
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False),
)
async def edit_keyword_block(model: str, action: str, anchor: int | str, text: str = "", expect_count: int = 1,
                             timeout: float | None = None) -> CallToolResult:
    if not model.strip():
        return _validation_error("model must not be empty")
    if action not in ("insert_after", "insert_before", "replace", "delete"):
        return _validation_error("action must be insert_after, insert_before, replace or delete")
    if isinstance(anchor, str):
        try:
            re.compile(anchor)
        except re.error as exc:
            return _validation_error(f"anchor is not a valid regular expression: {exc}")
    if action != "delete" and not text.strip():
        return _validation_error("text must not be empty")
    if expect_count < 1:
        return _validation_error("expect_count must be at least 1")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.EDIT_KEYWORD_BLOCK, model=model, action=action, anchor=anchor, text=text,
                              expect_count=expect_count)
    return await _operation(code, timeout)


@mcp.tool(
    description="Look up an Abaqus scripting method or object, for example Model.PorePressure, Part.Set, "
                "Step.setValues or mdb.models['Model-1'].SoilsStep. Returns __doc__ (Abaqus 2024 gives outdated "
                "keywords for some methods), the Python signature when available, and real calls of the method "
                "recorded in abaqus.rpy files (newest first) with the keywords they used. rpy files are read from "
                "rpy_dirs, or from the Abaqus/CAE startup directory and the current working directory." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def api_help(target: str, rpy_dirs: list[str] | None = None, max_examples: int = 5,
                   timeout: float | None = None) -> CallToolResult:
    if not target.strip():
        return _validation_error("target must not be empty")
    if any(not os.path.isabs(directory) for directory in rpy_dirs or []):
        return _validation_error("rpy_dirs must contain absolute directory paths")
    if not 1 <= max_examples <= 50:
        return _validation_error("max_examples must be between 1 and 50")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.API_HELP, target=target.strip(), rpy_dirs=list(rpy_dirs or []),
                              max_examples=max_examples)
    return await _operation(code, timeout)


@mcp.tool(
    description="List the jobs registered in the current CAE session with their status and settings." + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def list_jobs(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(_jobs_code("", False, None), timeout)


async def _job_status(name: str, since: float | None, directory: str = "", diagnostics: bool = True,
                      warning_detail: str = "", timeout: float | None = None) -> dict[str, Any]:
    execution = await _execute(_jobs_code(name, False, since, directory, diagnostics, warning_detail), timeout)
    if not execution.get("ok"):
        raise _StatusFailure(execution)
    return execution


class _StatusFailure(Exception):
    def __init__(self, execution: dict[str, Any]):
        super().__init__("job status failed")
        self.execution = execution


@mcp.tool(
    description="Submit an existing job of the current CAE session. With wait=true (default) the server polls the "
                "job with short calls until it finishes, so CAE stays responsive; timeout (seconds, default 3600) "
                "bounds the wait and the job keeps running when it elapses. With wait=false it returns as soon as "
                "the job is submitted or running; follow it with monitor_job_status using since=submittedAt and "
                "wait_seconds. datacheck=true runs a data check (Job.submit(datacheckJob=True)). Submitting is "
                "refused while the job's ODB is open in the session unless close_open_odb=true. The client's own "
                "tool timeout must be longer than the wait.",
)
async def submit_job(job_name: str, wait: bool = True, datacheck: bool = False, close_open_odb: bool = False,
                     timeout: float | None = None) -> CallToolResult:
    name = job_name.strip()
    if not name:
        return _validation_error("job_name must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    wait_limit = SUBMIT_WAIT_TIMEOUT if timeout is None else timeout
    if not wait:
        wait_limit = min(wait_limit, SUBMIT_START_SECONDS)
    try:
        submission = await _execute(kernel_code.render(kernel_code.SUBMIT_JOB, job_name=name, datacheck=datacheck,
                                                       close_open_odb=close_open_odb))
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    if not submission.get("ok"):
        return _execution_result(submission)
    submitted = submission["return_value"]
    started = time.monotonic()
    while True:
        await anyio.sleep(min(SUBMIT_POLL_SECONDS, max(0.0, wait_limit - (time.monotonic() - started))))
        finished_waiting = time.monotonic() - started >= wait_limit
        try:
            execution = await _job_status(name, submitted["submittedAt"], diagnostics=False)
        except BridgeError as exc:
            return _result({**_bridge_error(exc), "submission": submitted}, error=True)
        except _StatusFailure as failure:
            return _result({"ok": False, "executionId": failure.execution["executionId"],
                            "error": _python_error(failure.execution), "submission": submitted}, error=True)
        status = execution["return_value"]
        started_running = status["caeStatus"] in ("SUBMITTED", "RUNNING", "CHECK_SUBMITTED", "CHECK_RUNNING")
        if status["terminal"] or finished_waiting or (not wait and started_running):
            break
    if status["terminal"]:
        try:
            execution = await _job_status(name, submitted["submittedAt"])
        except BridgeError as exc:
            return _result({**_bridge_error(exc), "submission": submitted}, error=True)
        except _StatusFailure as failure:
            return _result({"ok": False, "executionId": failure.execution["executionId"],
                            "error": _python_error(failure.execution), "submission": submitted}, error=True)
        status = execution["return_value"]
    data: dict[str, Any] = {
        "ok": True,
        "executionId": submission["executionId"],
        "jobName": name,
        "datacheck": datacheck,
        "submittedAt": submitted["submittedAt"],
        "workingDirectory": submitted["workingDirectory"],
        "closedOdbs": submitted["closedOdbs"],
        "waitedSeconds": round(time.monotonic() - started, 1),
        "completed": status["terminal"],
        "state": status["state"],
        "status": status,
        "statusExecutionId": execution["executionId"],
    }
    if submission.get("stdout"):
        data["submitStdout"] = submission["stdout"]
    if not status["terminal"]:
        data["note"] = ("The job is still running. Follow it with monitor_job_status using job_name, "
                        "since=submittedAt and wait_seconds.")
    return _result(data)


def _until_met(until: str, first: dict[str, Any], current: dict[str, Any]) -> bool:
    match = UNTIL_PATTERN.match(until)
    if current["terminal"]:
        return True
    if match.group(2) is not None:
        return (current["currentStep"] or 0) >= int(match.group(2))
    if until == "changed":
        keys = ("state", "currentStep", "increment")
        modified = {ext: info["modifiedAt"] for ext, info in current["files"].items()}
        before = {ext: info["modifiedAt"] for ext, info in first["files"].items()}
        return any(first[key] != current[key] for key in keys) or modified != before
    return False


@mcp.tool(
    description="Without job_name, list the jobs. With job_name, combine the CAE status, the solver processes and "
                "the .log/.sta/.dat/.msg/.lck files to report the state (COMPLETED, FAILED, TERMINATED, RUNNING, "
                "SUBMITTED, DEAD when the files stop without an end marker and no solver process is left, or "
                "UNKNOWN), progress, and errors and warnings grouped by their text with counts over the whole .msg "
                "and .dat files. directory looks at a job folder other than the CAE working directory without "
                "changing it; jobs there and jobs that the current CAE session did not submit are judged from the "
                "files and solver processes. since (Unix seconds) binds file-only evidence to one run. wait_seconds "
                "waits until the condition until holds: terminal (default), step>=N or changed. warning_detail "
                "takes the key of one error or warning group and lists every occurrence. terminate=true sends the "
                "Abaqus terminate command for a job of the current CAE session; confirm the TERMINATED state with a "
                "later call. The client's own tool timeout must be longer than wait_seconds." + TIMEOUT_NOTE,
)
async def monitor_job_status(job_name: str = "", terminate: bool = False, since: float | None = None,
                             directory: str = "", wait_seconds: float = 0.0, until: str = "terminal",
                             warning_detail: str = "", timeout: float | None = None) -> CallToolResult:
    name = job_name.strip()
    if terminate and not name:
        return _validation_error("job_name is required for termination")
    if terminate and directory:
        return _validation_error("terminate works for jobs of the current CAE session; leave directory empty")
    if since is not None and not 0 <= since <= 4102444800:
        return _validation_error("since must be a Unix timestamp between 1970 and 2100")
    if directory and not os.path.isabs(directory):
        return _validation_error("directory must be an absolute path")
    if _invalid_wait(wait_seconds):
        return _validation_error(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS:g}")
    if not UNTIL_PATTERN.match(until):
        return _validation_error("until must be terminal, changed or step>=N")
    if (wait_seconds or warning_detail or directory) and not name:
        return _validation_error("job_name is required for directory, wait_seconds and warning_detail")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    if not name or terminate or not wait_seconds:
        return await _operation(_jobs_code(name, terminate, since, directory, True, warning_detail), timeout)
    started = time.monotonic()
    try:
        first = (await _job_status(name, since, directory, False, timeout=timeout))["return_value"]
        current = first
        while not _until_met(until, first, current) and time.monotonic() - started < wait_seconds:
            await anyio.sleep(min(SUBMIT_POLL_SECONDS, max(0.0, wait_seconds - (time.monotonic() - started))))
            current = (await _job_status(name, since, directory, False, timeout=timeout))["return_value"]
        final = await _job_status(name, since, directory, True, warning_detail, timeout)
    except BridgeError as exc:
        return _result(_bridge_error(exc), error=True)
    except _StatusFailure as failure:
        return _execution_result(failure.execution)
    data = _operation_data(final)
    data["waitedSeconds"] = round(time.monotonic() - started, 1)
    data["until"] = until
    data["conditionMet"] = _until_met(until, first, data)
    return _result(data)


@mcp.tool(
    description="Inspect an ODB read-only: steps (start, end and duration times), sampled frames, field outputs with "
                "components, invariants and positions, history regions and sets. Optionally summarize one field "
                "(min/max) at step/frame/set/component, or return a sampled history curve by region and variable. "
                "Vector fields without component use the magnitude; tensor fields need a component. An ODB already "
                "open in the session (openBeforeCall) is read and left open; otherwise it is opened read-only and "
                "closed again." + TIMEOUT_NOTE,
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
    description="List the ODBs open in the Abaqus/CAE session with their path, read-only flag and the viewports that "
                "display them. Open ODBs lock their files, so the folders holding them cannot be cleaned up."
                + TIMEOUT_NOTE,
    annotations=READ_ONLY,
)
async def list_open_odbs(timeout: float | None = None) -> CallToolResult:
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    return await _operation(kernel_code.LIST_ODBS, timeout)


@mcp.tool(
    description="Close the ODBs open in the Abaqus/CAE session whose path matches pattern (fnmatch, case-insensitive, "
                "for example *\\trthru.odb or E:\\work\\*). ODBs shown in a viewport may be what the user is looking "
                "at; they are skipped unless include_displayed=true." + TIMEOUT_NOTE,
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def close_odbs(pattern: str, include_displayed: bool = False, timeout: float | None = None) -> CallToolResult:
    if not pattern.strip():
        return _validation_error("pattern must not be empty")
    if _invalid_timeout(timeout):
        return _validation_error(TIMEOUT_MESSAGE)
    code = kernel_code.render(kernel_code.CLOSE_ODBS, pattern=pattern.strip(), include_displayed=include_displayed)
    return await _operation(code, timeout)


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
    client = _client()
    try:
        session = await anyio.to_thread.run_sync(lambda: client.ping(5.0, SESSION_NAMESPACE))
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

import ast
import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio
import pytest

import abaqus_mcp
from abaqus_mcp import server
from abaqus_mcp.client import AbaqusBridgeClient, BridgeError
from abaqus_mcp.kernel_wrapper import PROTOCOL
from abaqus_mcp.protocol import read_message, send_message
from support import (BRIDGE_PORT, BUILD_MODEL, IDLE_PORT, LOOSE_BLOCK, PROJECT_ROOT, REMOVE_MODEL, RUN_ROOT,
                     SCRIPTS_DIR, UNLOAD_STEP, call_ok, mcp_session, model_code, payload)

EXPECTED_TOOLS = {
    "ping", "check_abaqus_connection", "run_python", "execute_script", "run_script_file", "reset_namespace",
    "get_execution", "set_workdir", "get_model_info", "get_step_states", "write_input", "find_keyword_blocks",
    "edit_keyword_block", "api_help", "list_jobs", "submit_job", "monitor_job_status", "inspect_odb", "get_odb_info",
    "list_open_odbs", "close_odbs", "capture_viewport", "get_viewport_image",
}
EXPECTED_RESOURCES = {"abaqus://session-telemetry", "abaqus://status", "abaqus://agent-instructions"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PYTHON2_LAUNCHER = Path(r"C:\SIMULIA\Commands2021\abq2021.bat")
SHARED_NAMESPACE = "mcp-test-shared"


def _run_cli(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPTS_DIR / f"{name}.exe"), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=300)


async def _current_workdir(session) -> str:
    return (await call_ok(session, "run_python", {"code": "import os\nresult = os.getcwd()"}))["value"]


def _case_dir(prefix: str) -> Path:
    directory = RUN_ROOT / f"{prefix}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    return directory


@pytest.mark.anyio
async def test_tools_and_resources_are_listed():
    async with mcp_session() as session:
        tools = (await session.list_tools()).tools
        resources = (await session.list_resources()).resources
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert "timeout" in tool.input_schema["properties"], tool.name
    assert {str(resource.uri) for resource in resources} == EXPECTED_RESOURCES


def test_plugin_version_and_protocol_match_package():
    tree = ast.parse((PROJECT_ROOT / "abaqus_mcp" / "gui_plugin.py").read_text(encoding="utf-8"))
    values = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and isinstance(node.value, ast.Constant) for target in node.targets if isinstance(target, ast.Name)}
    assert values["__version__"] == abaqus_mcp.__version__
    assert values["PROTOCOL"] == PROTOCOL


@pytest.mark.anyio
async def test_arguments_are_validated_before_reaching_abaqus():
    cases = [
        ("run_python", {"code": "   "}, "code must not be empty"),
        ("run_python", {"code": "1", "timeout": 0}, "timeout must be"),
        ("run_python", {"code": "1", "namespace": " "}, "namespace must not be empty"),
        ("ping", {"timeout": -1}, "timeout must be"),
        ("execute_script", {"script": ""}, "script must not be empty"),
        ("run_script_file", {"path": "relative.py"}, "path must be an absolute path"),
        ("run_script_file", {"path": str(RUN_ROOT / "a.py"), "result_variable": "1x"}, "result_variable must be"),
        ("run_script_file", {"path": str(RUN_ROOT / "a.py"), "cwd": "relative"}, "cwd must be an absolute path"),
        ("run_script_file", {"path": str(RUN_ROOT / "a.py"), "fresh_modules": ["lib"]}, "fresh_modules must contain"),
        ("reset_namespace", {"namespace": ""}, "namespace must not be empty"),
        ("get_execution", {"execution_id": " "}, "execution_id must not be empty"),
        ("get_execution", {"execution_id": "x", "wait_seconds": -1}, "wait_seconds must be"),
        ("set_workdir", {"path": " "}, "path must not be empty"),
        ("get_step_states", {"model": ""}, "model must not be empty"),
        ("get_step_states", {"model": "M", "kinds": ["springs"]}, "unknown kinds"),
        ("get_step_states", {"model": "M", "max_entries": 0}, "max_entries must be"),
        ("write_input", {"model": " "}, "model must not be empty"),
        ("write_input", {"model": "M", "directory": "relative"}, "directory must be an absolute path"),
        ("write_input", {"model": "M", "max_data_lines": -1}, "max_data_lines must be"),
        ("write_input", {"model": "M", "max_blocks": 0}, "max_blocks must be"),
        ("write_input", {"model": "M", "name_filter": "("}, "name_filter is not a valid regular expression"),
        ("find_keyword_blocks", {"model": "M", "pattern": "("}, "pattern is not a valid regular expression"),
        ("find_keyword_blocks", {"model": "M", "pattern": "x", "context": 11}, "context must be 0-10"),
        ("edit_keyword_block", {"model": "M", "action": "append", "anchor": 1, "text": "*X"}, "action must be"),
        ("edit_keyword_block", {"model": "M", "action": "replace", "anchor": 1}, "text must not be empty"),
        ("edit_keyword_block", {"model": "M", "action": "delete", "anchor": "(("}, "anchor is not a valid"),
        ("edit_keyword_block", {"model": "M", "action": "delete", "anchor": 1, "expect_count": 0},
         "expect_count must be"),
        ("api_help", {"target": ""}, "target must not be empty"),
        ("api_help", {"target": "Part.Set", "rpy_dirs": ["relative"]}, "rpy_dirs must contain"),
        ("api_help", {"target": "Part.Set", "max_examples": 0}, "max_examples must be"),
        ("submit_job", {"job_name": ""}, "job_name must not be empty"),
        ("monitor_job_status", {"terminate": True}, "job_name is required"),
        ("monitor_job_status", {"job_name": "x", "since": -5}, "since must be"),
        ("monitor_job_status", {"job_name": "x", "terminate": True, "directory": str(RUN_ROOT)},
         "terminate works for jobs of the current CAE session"),
        ("monitor_job_status", {"job_name": "x", "directory": "relative"}, "directory must be an absolute path"),
        ("monitor_job_status", {"job_name": "x", "wait_seconds": -1}, "wait_seconds must be"),
        ("monitor_job_status", {"job_name": "x", "until": "done"}, "until must be"),
        ("monitor_job_status", {"directory": str(RUN_ROOT)}, "job_name is required for directory"),
        ("inspect_odb", {"odb_path": ""}, "odb_path must not be empty"),
        ("inspect_odb", {"odb_path": "a.odb", "max_points": 1}, "max_points must be"),
        ("get_odb_info", {"odb_path": ""}, "odb_path must not be empty"),
        ("close_odbs", {"pattern": " "}, "pattern must not be empty"),
        ("capture_viewport", {"image_format": "JPG"}, "image_format must be"),
        ("get_viewport_image", {"image_format": "SVG"}, "image_format must be"),
    ]
    async with mcp_session() as session:
        for tool, arguments, message in cases:
            result = await session.call_tool(tool, arguments)
            data = payload(result)
            assert result.is_error, (tool, data)
            assert data["error"]["type"] == "ValueError" and message in data["error"]["message"], (tool, data)


@pytest.mark.anyio
async def test_ping_and_connection_text():
    async with mcp_session() as session:
        data = await call_ok(session, "ping")
        text = await session.call_tool("check_abaqus_connection", {})
    info = data["session"]
    assert data["ok"] is True and data["executionId"] == info["executionId"]
    assert info["abaqus_version"].isdigit()
    assert isinstance(info["models"], list) and isinstance(info["viewports"], list)
    assert os.path.isabs(info["cwd"])
    bridge = info["bridge"]
    assert bridge["version"] == abaqus_mcp.__version__ and bridge["port"] == BRIDGE_PORT and bridge["running"] is True
    assert bridge["protocol"] == PROTOCOL and isinstance(bridge["autostart"], bool)
    assert isinstance(bridge["processed"], int) and bridge["gui_thread"] == "MainThread"
    assert os.path.isabs(bridge["gui_cwd"])
    assert info["namespace"]["name"].startswith("conversation-")
    assert not text.is_error
    assert text.content[0].text.startswith(
        f"Connected to Abaqus/CAE {info['abaqus_version']} through the MCP bridge at 127.0.0.1:{BRIDGE_PORT} "
        f"(bridge {abaqus_mcp.__version__}).")


@pytest.mark.anyio
async def test_refused_connection_names_the_running_cae():
    # CAE 正在运行但端口上没有桥接：说明应指出 CAE 的进程号并提示启动桥接
    async with mcp_session(IDLE_PORT) as session:
        result = await session.call_tool("ping", {})
    data = payload(result)
    assert result.is_error and data["state"] == "NOT_STARTED", data
    assert data["error"]["type"] == "ConnectionRefusedError"
    assert "Abaqus/CAE is running (GUI process" in data["error"]["message"]
    assert "Start MCP Bridge" in data["error"]["hint"] and "ABAQUS_MCP_AUTOSTART" in data["error"]["hint"]


def test_plugin_rejects_other_protocols():
    execution_id = str(uuid.uuid4())
    message = {"id": execution_id, "method": "run",
               "params": {"protocol": PROTOCOL - 1, "executionId": execution_id, "timeout": 10,
                          "deadline": time.time() + 15}}
    with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=20) as sock:
        send_message(sock, message)
        response = read_message(sock, 1 << 20)
    assert response["ok"] is False and "Protocol mismatch" in response["error"]["message"], response
    with pytest.raises(BridgeError) as raised:
        AbaqusBridgeClient.from_environment().request("execute", {"code": "1"}, 10)
    assert raised.value.error_type == "PluginMismatch" and "abaqus-mcp-setup" in raised.value.hint


@pytest.mark.anyio
async def test_run_python_returns_values_and_clears_result():
    async with mcp_session() as session:
        assert (await call_ok(session, "run_python", {"code": "1 + 1"}))["value"] == 2
        assert (await call_ok(session, "run_python", {"code": "result = {'a': 1}"}))["value"] == {"a": 1}
        assert (await call_ok(session, "run_python", {"code": "mcp_test_value = 21"}))["value"] is None
        assert (await call_ok(session, "run_python", {"code": "mcp_test_value * 2"}))["value"] == 42
        printed = await call_ok(session, "run_python", {"code": "print('hello from abaqus')\nresult = 3"})
        assert printed["value"] == 3 and printed["stdout"] == "hello from abaqus\n"


@pytest.mark.anyio
async def test_results_are_converted_to_json():
    code = ("import collections, numpy, sys\n"
            "result = {'flag': ON, 'off': OFF, 'inc': STEP_END, 'f32': numpy.float32(1.5), "
            "'arr': numpy.array([1.0, 2.0], 'f'), ('a', 1): 3, 'nan': float('nan'), 'inf': float('-inf'), "
            "'max': sys.float_info.max, 'counter': collections.Counter([('x', 1), ('x', 1)]), 'model': mdb}")
    async with mcp_session() as session:
        result = await session.call_tool("run_python", {"code": code})
    # 结果必须是严格的 JSON：不能出现 NaN、Infinity 这类 JSON 不允许的数值
    data = json.loads(result.content[0].text, parse_constant=lambda name: pytest.fail(f"non-JSON constant {name}"))
    assert not result.is_error, data
    value = data["value"]
    assert value["flag"] is True and value["off"] is False and value["inc"] == "STEP_END"
    assert value["f32"] == 1.5 and value["arr"] == [1.0, 2.0] and value["('a', 1)"] == 3
    assert value["nan"] == "NaN" and value["inf"] == "-Infinity" and float(value["max"]) == sys.float_info.max
    assert value["counter"] == {"('x', 1)": 2}
    assert value["model"] == {"repr": "mdb", "type": "abaqus.Mdb"}
    assert {item["path"] for item in data["replaced"]} == {"value.nan", "value.inf", "value.max", "value.model"}


@pytest.mark.anyio
async def test_run_python_errors_carry_location_and_suggestions():
    async with mcp_session() as session:
        missing = await session.call_tool("run_python", {"code": "x = 1\nmodel = mdb.models['NoSuchModel']"})
        typo = await session.call_tool("run_python", {"code": "mdbb.models.keys()"})
        # AbaqusNameError 的正文是按 GBK 解码的 UTF-8 字节，返回时应恢复为原文
        unnamed = await session.call_tool("run_python", {"code": "mdb.Model(nme='X')"})
        keyword = await session.call_tool("run_python", {
            "code": "session.viewports[session.currentViewportName].setValues(badKeyword=1)"})
        helper = await call_ok(session, "run_python", {
            "code": "try:\n    mdb.Model(nme='X')\nexcept Exception as e:\n    result = abaqus_error_text(e)"})
    missing_data = payload(missing)
    assert missing.is_error and missing_data["ok"] is False
    assert missing_data["error"]["type"] == "KeyError" and missing_data["error"]["at"]["line"] == 2
    suggestions = missing_data["error"]["suggestions"]
    assert suggestions["missing_key"] == "NoSuchModel"
    assert suggestions.get("possible_keys", []) + suggestions.get("available_keys_sample", [])
    typo_data = payload(typo)
    assert typo.is_error and typo_data["error"]["type"] == "NameError"
    assert "mdb" in typo_data["error"]["suggestions"]["possible_names"]
    unnamed_error = payload(unnamed)["error"]
    assert unnamed_error["type"] == "AbaqusNameError" and unnamed_error["message"] == "名称为空", unnamed_error
    assert unnamed_error["raw_message"] == "名称为空".encode("utf-8").decode("gbk")
    keyword_error = payload(keyword)["error"]
    assert keyword_error["type"] == "TypeError" and "badKeyword" in keyword_error["message"]
    assert keyword_error["suggestions"]["invalid_keyword"] == "badKeyword"
    assert keyword_error["suggestions"]["signature_source"].startswith("__doc__")
    assert helper["value"] == "名称为空"


@pytest.mark.anyio
async def test_namespaces_are_preloaded_per_conversation_and_resettable():
    async with mcp_session() as first, mcp_session() as second:
        preloaded = await call_ok(first, "run_python", {
            "code": "THREE_D, regionToolset.__name__, type(part).__name__, sum(x for x in range(4)), "
                    "abaqus.sum is not sum, pow(2, 3)"})
        assert preloaded["value"] == ["THREE_D", "regionToolset", "module", 6, True, 8]
        assert preloaded["namespace"]["created"] is True
        await call_ok(first, "run_python", {"code": "mcp_namespace_value = 1"})
        assert (await call_ok(first, "run_python", {"code": "'mcp_namespace_value' in dir()"}))["value"] is True
        assert (await call_ok(second, "run_python", {"code": "'mcp_namespace_value' in dir()"}))["value"] is False

        await call_ok(first, "run_python", {"code": "mcp_shared_value = 'from first'", "namespace": SHARED_NAMESPACE})
        shared = await call_ok(second, "run_python", {"code": "mcp_shared_value", "namespace": SHARED_NAMESPACE})
        assert shared["value"] == "from first"

        shadowed = await call_ok(first, "run_python", {"code": "part = 1\nresult = part"})
        assert shadowed["value"] == 1 and "no longer to the Abaqus module part" in shadowed["warnings"][0]
        reset = await call_ok(first, "reset_namespace")
        assert reset["value"] == {"reset": True} and reset["namespace"]["created"] is True
        after = await call_ok(first, "run_python", {"code": "type(part).__name__, 'mcp_namespace_value' in dir()"})
        assert after["value"] == ["module", False]
        await call_ok(second, "reset_namespace", {"namespace": SHARED_NAMESPACE})
        assert (await call_ok(second, "run_python", {"code": "'mcp_shared_value' in dir()",
                                                     "namespace": SHARED_NAMESPACE}))["value"] is False


@pytest.mark.anyio
async def test_execute_script_returns_text():
    async with mcp_session() as session:
        printed = await session.call_tool("execute_script", {"script": "print('line')\nresult = {'k': 1}"})
        quiet = await session.call_tool("execute_script", {"script": "mcp_quiet_value = 1"})
        failed = await session.call_tool("execute_script", {"script": "1 / 0"})
    assert not printed.is_error and printed.content[0].text == 'line\n\n{\n  "k": 1\n}'
    assert not quiet.is_error and quiet.content[0].text == "(Script executed successfully, no output)"
    assert failed.is_error and payload(failed)["error"]["type"] == "ZeroDivisionError"


@pytest.mark.anyio
async def test_run_script_file_like_nogui():
    case = _case_dir("script")
    helper = case / "mcp_helper_module.py"
    script = case / "build_case.py"
    helper.write_text("VALUE = 'v1'\n", encoding="utf-8")
    script.write_text(
        "import os, sys\nimport mcp_helper_module\n"
        "BUILD_RESULT = {'file': __file__, 'name': __name__, 'argv': sys.argv[1:], 'job': JOB, "
        "'helper': mcp_helper_module.VALUE, 'cwd': os.getcwd()}\n"
        "if JOB == 'fail':\n    raise ValueError('中文错误')\n", encoding="utf-8")
    async with mcp_session() as session:
        original = await _current_workdir(session)
        first = await call_ok(session, "run_script_file", {
            "path": str(script), "args": ["a b", "中文"], "variables": {"JOB": "trthru"},
            "result_variable": "BUILD_RESULT", "cwd": str(case)})
        assert first["value"] == {"file": str(script), "name": "__main__", "argv": ["a b", "中文"], "job": "trthru",
                                  "helper": "v1", "cwd": str(case)}
        assert first["script"]["path"] == str(script)
        assert await _current_workdir(session) == original

        # 同一秒内改写且大小不变：只有删除缓存的 .pyc 才能读到新代码
        helper.write_text("VALUE = 'v2'\n", encoding="utf-8")
        cached = await call_ok(session, "run_script_file", {
            "path": str(script), "variables": {"JOB": "x"}, "result_variable": "BUILD_RESULT"})
        assert cached["value"]["helper"] == "v1" and cached["value"]["argv"] == []
        fresh = await call_ok(session, "run_script_file", {
            "path": str(script), "variables": {"JOB": "x"}, "result_variable": "BUILD_RESULT",
            "fresh_modules": [str(case)]})
        assert fresh["value"]["helper"] == "v2" and fresh["script"]["freshModules"] == ["mcp_helper_module"]

        failed = await session.call_tool("run_script_file", {
            "path": str(script), "variables": {"JOB": "fail"}, "result_variable": "BUILD_RESULT"})
        error = payload(failed)["error"]
        assert failed.is_error and error["type"] == "ValueError" and error["message"] == "中文错误"
        assert error["at"]["file"] == str(script) and error["at"]["line"] == 5
        assert (await call_ok(session, "run_python", {"code": "last_script_globals['JOB']"}))["value"] == "fail"
        missing = await session.call_tool("run_script_file", {
            "path": str(script), "variables": {"JOB": "x"}, "result_variable": "NOT_SET"})
        assert missing.is_error and "finished without defining 'NOT_SET'" in payload(missing)["error"]["message"]


@pytest.mark.anyio
async def test_set_workdir_round_trip():
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    async with mcp_session() as session:
        original = await _current_workdir(session)
        try:
            moved = await call_ok(session, "set_workdir", {"path": str(RUN_ROOT)})
            assert moved["previous"] == original and Path(moved["current"]) == RUN_ROOT
            relative = await session.call_tool("set_workdir", {"path": "relative/dir"})
            assert relative.is_error and "path must be absolute" in payload(relative)["error"]["message"]
            missing = await session.call_tool("set_workdir", {"path": str(RUN_ROOT / f"missing-{uuid.uuid4().hex}")})
            assert missing.is_error and payload(missing)["error"]["type"] == "FileNotFoundError"
        finally:
            restored = await call_ok(session, "set_workdir", {"path": original})
    assert restored["current"] == original


@pytest.mark.anyio
async def test_step_states_and_input_keywords():
    model_name, job_name = "McpTestSteps", "McpTestStepsJob"
    case = _case_dir("inp")
    async with mcp_session() as session:
        original = await _current_workdir(session)
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name)})
            steps = await call_ok(session, "run_python", {"code": model_code(UNLOAD_STEP, model_name, job_name)})
            assert steps["value"] == ["Initial", "Load", "Unload"]

            states = await call_ok(session, "get_step_states", {"model": model_name})
            assert states["stepOrder"] == ["Initial", "Load", "Unload"]
            by_step = {step["name"]: step for step in states["steps"]}
            assert by_step["Initial"]["created"] == {"boundary_conditions": ["Fix"]}
            assert by_step["Load"]["created"]["loads"] == ["Tip"]
            unload = by_step["Unload"]
            assert unload["created"] == {"interactions": ["Remove"]} and unload["modified"] == {"loads": ["Tip"]}
            assert unload["deactivated"] == {"boundary_conditions": ["Fix"]}
            # Abaqus 默认的 DBL_MAX 以完整精度的文本返回
            assert float(unload["attributes"]["stabilizationMagnitude"]) == sys.float_info.max
            objects = {(item["kind"], item["name"]): item for item in states["objects"]}
            tip = objects[("loads", "Tip")]
            assert tip["createStep"] == "Load"
            assert [(entry["step"], entry["status"], entry["values"]) for entry in tip["steps"]] == [
                ("Load", "CREATED", {"magnitude": 1.0}), ("Unload", "MODIFIED", {"magnitude": 0.5})]
            assert objects[("boundary_conditions", "Fix")]["createStep"] == "Initial"
            assert objects[("interactions", "Remove")]["type"] == "ModelChange"
            filtered = await call_ok(session, "get_step_states",
                                     {"model": model_name, "kinds": ["loads"], "steps": ["Unload"]})
            assert [step["name"] for step in filtered["steps"]] == ["Unload"] and filtered["objectCount"] == 1
            assert [entry["step"] for entry in filtered["objects"][0]["steps"]] == ["Unload"]
            missing = await session.call_tool("get_step_states", {"model": "NoSuchMcpModel"})
            assert missing.is_error and payload(missing)["error"]["type"] == "KeyError"

            written = await call_ok(session, "write_input", {
                "model": model_name, "directory": str(case), "keywords": ["*Material", "*Boundary", "*Model Change"]})
            assert Path(written["path"]).is_file() and Path(written["path"]).parent == case
            assert written["keywordCounts"]["modelchange"] == 1 and written["keywordCounts"]["boundary"] == 2
            matched = [(item["keyword"], item["parameters"]) for item in written["matches"]]
            assert matched == [("material", {"name": "STEEL"}), ("boundary", {}), ("boundary", {"op": "NEW"}),
                               ("modelchange", {"remove": True})]
            assert written["matches"][1]["data"] == [["Beam-1.FIXED", "ENCASTRE"]]
            assert await _current_workdir(session) == original
            listed = {job["name"] for job in (await call_ok(session, "list_jobs"))["jobs"]}
            assert not any(name.startswith("mcp_inp_") for name in listed)
            counts = await call_ok(session, "write_input", {"model": model_name, "directory": str(case),
                                                            "keep_file": False})
            assert counts["path"] is None and counts["matches"] == [] and counts["keywordCounts"]["step"] == 2
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})


@pytest.mark.anyio
async def test_keyword_block_find_and_edit():
    model_name, job_name = "McpTestKeywords", "McpTestKeywordsJob"
    monitor = "*Monitor, dof=1, node=Beam-1.1, frequency=7"
    async with mcp_session() as session:
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name)})
            await call_ok(session, "run_python", {"code": model_code(UNLOAD_STEP, model_name, job_name)})
            found = await call_ok(session, "find_keyword_blocks",
                                  {"model": model_name, "pattern": r"^\*End Step", "context": 1})
            assert found["matchCount"] == 2 and all(match["text"] == "*End Step" for match in found["matches"])
            assert found["matches"][0]["before"][0]["firstLine"].startswith("*Output")
            total = found["blockCount"]

            single = await session.call_tool("edit_keyword_block", {
                "model": model_name, "action": "insert_before", "anchor": r"^\*End Step", "text": monitor})
            assert single.is_error and "anchor matched 2 blocks but expect_count is 1" in payload(single)["error"]["message"]
            inserted = await call_ok(session, "edit_keyword_block", {
                "model": model_name, "action": "insert_before", "anchor": r"^\*End Step", "text": monitor,
                "expect_count": 2})
            assert inserted["blocksBefore"] == total and inserted["blocksAfter"] == total + 2
            again = await session.call_tool("edit_keyword_block", {
                "model": model_name, "action": "insert_before", "anchor": r"^\*End Step", "text": monitor,
                "expect_count": 2})
            assert again.is_error and "identical block already exists" in payload(again)["error"]["message"]
            placed = await call_ok(session, "find_keyword_blocks", {"model": model_name, "pattern": "frequency=7",
                                                                    "context": 1})
            assert [match["after"][0]["firstLine"] for match in placed["matches"]] == ["*End Step", "*End Step"]

            replaced = await call_ok(session, "edit_keyword_block", {
                "model": model_name, "action": "replace", "anchor": "frequency=7",
                "text": "*Monitor, dof=2, node=Beam-1.1, frequency=8", "expect_count": 2})
            assert replaced["blocksAfter"] == total + 2
            written = await call_ok(session, "write_input", {"model": model_name, "keywords": ["*Monitor"],
                                                             "directory": str(_case_dir("keywords")),
                                                             "keep_file": False})
            assert [item["parameters"] for item in written["matches"]] == [
                {"dof": 2, "node": "BEAM-1.1", "frequency": 8}] * 2

            deleted = await call_ok(session, "edit_keyword_block", {
                "model": model_name, "action": "delete", "anchor": "frequency=8", "expect_count": 2})
            assert deleted["blocksAfter"] == total
            assert (await call_ok(session, "find_keyword_blocks",
                                  {"model": model_name, "pattern": r"\*Monitor"}))["matchCount"] == 0

            nodes = await call_ok(session, "edit_keyword_block", {
                "model": model_name, "action": "insert_after", "anchor": inserted["positions"][0] - 1,
                "text": "*Node, nset=MCPTEST\n90001, 0., 0., 0."})
            assert "*Node or *Element data" in nodes["warnings"][0]
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})


@pytest.mark.anyio
async def test_api_help_reads_doc_and_rpy_calls():
    model_name, job_name = "McpTestApi", "McpTestApiJob"
    case = _case_dir("rpy")
    # abaqus.rpy 按 mbcs（本机为 GBK）编码写入，并带有 coding 声明
    (case / "abaqus.rpy").write_bytes((
        "# -*- coding: mbcs -*-\n"
        "# 中文注释\n"
        "p = mdb.models['M'].parts['P']\n"
        "p.SectionAssignment(region=region, sectionName='衬砌', offset=0.0,\n"
        "    offsetType=MIDDLE_SURFACE, thicknessAssignment=FROM_SECTION)\n"
        "p.SectionAssignment(region=region2, sectionName='ground')\n").encode("gbk"))
    (case / "abaqus.rpy.1").write_text("p.SectionAssignment(region=r, sectionName='old'\n", encoding="utf-8")
    async with mcp_session() as session:
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name)})
            helped = await call_ok(session, "api_help", {
                "target": f"mdb.models[{model_name!r}].parts['Beam'].SectionAssignment", "rpy_dirs": [str(case)]})
            assert helped["resolvedType"] == "AbaqusMethod" and "sectionName" in helped["doc"]
            rpy = helped["rpy"]
            assert rpy["directories"] == [str(case)] and len(rpy["files"]) == 2
            assert [example["line"] for example in rpy["examples"]] == [4, 6]
            assert "sectionName='衬砌'" in rpy["examples"][0]["code"]
            assert rpy["keywordUsage"] == {"region": 2, "sectionName": 2, "offset": 1, "offsetType": 1,
                                           "thicknessAssignment": 1}
            assert [item["file"] for item in rpy["unreadable"]] == [str(case / "abaqus.rpy.1")]
            limited = await call_ok(session, "api_help", {
                "target": f"mdb.models[{model_name!r}].parts['Beam'].SectionAssignment", "rpy_dirs": [str(case)],
                "max_examples": 1})
            assert len(limited["rpy"]["examples"]) == 1
            root = await call_ok(session, "api_help", {"target": "Model.PorePressure", "rpy_dirs": [str(case)]})
            assert root["doc"].startswith("Model.PorePressure(") and root["rpy"]["examples"] == []
            default_dirs = await call_ok(session, "api_help", {"target": "Model.PorePressure"})
            gui_cwd = (await call_ok(session, "ping"))["session"]["bridge"]["gui_cwd"]
            assert default_dirs["rpy"]["directories"][0] == gui_cwd
            unknown = await session.call_tool("api_help", {"target": "Nothing.at.all"})
            assert unknown.is_error and "target must start with" in payload(unknown)["error"]["message"]
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})


@pytest.mark.anyio
async def test_model_job_odb_and_viewport_workflow():
    model_name, job_name = "McpTestShort", "McpTestShortJob"
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    async with mcp_session() as session:
        original = await _current_workdir(session)
        await call_ok(session, "set_workdir", {"path": str(RUN_ROOT)})
        try:
            built = await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name)})
            assert built["value"]["elements"] == 80

            info = await call_ok(session, "get_model_info")
            model = info["models"][model_name]
            assert model["parts"] == ["Beam"] and model["steps"] == ["Initial", "Load"]
            assert model["boundary_conditions"] == ["Fix"] and model["loads"] == ["Tip"]
            assert model["sets"] == ["TIP_ASM"] and model["counts"]["sets"] == 1
            beam = model["part_details"]["Beam"]
            assert beam["cells"] == 1 and beam["faces"] == 6 and set(beam["sets"]) == {"ALL", "FIXED", "TIPFACE"}
            assert info["list_limit"] == 200 and info["truncated"] == []
            listed = await call_ok(session, "list_jobs")
            job_entry = next(job for job in listed["jobs"] if job["name"] == job_name)
            assert job_entry["model"] == model_name and job_entry["numCpus"] == "1" and job_entry["caeStatus"] is None

            checked = await call_ok(session, "submit_job", {"job_name": job_name, "datacheck": True, "timeout": 600})
            assert checked["datacheck"] is True and checked["completed"] is True, checked
            assert checked["status"]["caeStatus"] == "CHECK_COMPLETED"

            submitted = await call_ok(session, "submit_job", {"job_name": job_name, "timeout": 600})
            assert submitted["completed"] is True and submitted["state"] == "COMPLETED", submitted
            assert submitted["status"]["fileState"] == "COMPLETED" and submitted["status"]["source"] == "CAE job status"
            assert submitted["closedOdbs"] == []

            monitored = await call_ok(session, "monitor_job_status",
                                      {"job_name": job_name, "since": submitted["submittedAt"]})
            assert monitored["state"] == "COMPLETED" and monitored["terminal"] is True
            assert monitored["runFreshness"] == "confirmed by CAE" and monitored["files"]["lck"]["exists"] is False
            assert monitored["errors"] == [] and monitored["errorCount"] == 0
            listing = await call_ok(session, "monitor_job_status")
            assert next(job for job in listing["jobs"] if job["name"] == job_name)["caeStatus"] == "COMPLETED"

            odb_path = str(RUN_ROOT / f"{job_name}.odb")
            meta = await call_ok(session, "inspect_odb", {"odb_path": odb_path})
            assert [step["name"] for step in meta["steps"]] == ["Load"] and meta["steps"][0]["duration"] == 1.0
            assert meta["openBeforeCall"] == []
            fields = {field["name"]: field for field in meta["steps"][0]["fieldOutputs"]}
            assert fields["U"]["positions"] == ["NODAL"] and fields["U"]["validInvariants"] == ["MAGNITUDE"]
            assert fields["S"]["positions"] == ["INTEGRATION_POINT"] and "MISES" in fields["S"]["validInvariants"]
            legacy = await call_ok(session, "get_odb_info", {"odb_path": odb_path})
            assert legacy["steps"] == meta["steps"]

            axial = await call_ok(session, "inspect_odb", {"odb_path": odb_path, "variable": "U", "component": "U1"})
            assert axial["fieldSummary"]["quantity"] == "component" and axial["fieldSummary"]["min"] < 0
            magnitude = await call_ok(session, "inspect_odb", {"odb_path": odb_path, "variable": "U"})
            assert magnitude["fieldSummary"]["quantity"] == "magnitude" and magnitude["fieldSummary"]["min"] >= 0
            tip = await call_ok(session, "inspect_odb",
                                {"odb_path": odb_path, "variable": "U", "component": "U1", "set_name": "TIP_ASM"})
            assert tip["fieldSummary"]["count"] > 0 and tip["fieldSummary"]["max"] < 0
            stress = await call_ok(session, "inspect_odb",
                                   {"odb_path": odb_path, "variable": "S", "component": "S11", "set_name": "BEAM-1.ALL"})
            assert stress["fieldSummary"]["count"] > 0
            tensor = await session.call_tool("inspect_odb", {"odb_path": odb_path, "variable": "S"})
            assert tensor.is_error and "Tensor field requires a component" in payload(tensor)["error"]["message"]
            history = await call_ok(session, "inspect_odb", {"odb_path": odb_path, "history_region": "Assembly ASSEMBLY",
                                                             "history_variable": "ALLIE"})
            assert history["historySummary"]["pointCount"] >= 1

            # 会话中打开的 ODB：inspect_odb 读取后保持打开，提交前被拒绝，close_open_odb 或 close_odbs 关闭
            opened = await call_ok(session, "run_python", {
                "code": f"mcp_test_odb = odbAccess.openOdb({odb_path!r}, readOnly=True)\nresult = mcp_test_odb.path"})
            key = opened["value"]
            assert [item["key"] for item in (await call_ok(session, "list_open_odbs"))["odbs"]] == [key]
            reused = await call_ok(session, "inspect_odb", {"odb_path": odb_path})
            assert reused["openBeforeCall"] == [key]
            assert [item["key"] for item in (await call_ok(session, "list_open_odbs"))["odbs"]] == [key]
            refused = await session.call_tool("submit_job", {"job_name": job_name, "timeout": 600})
            assert refused.is_error and "is open in this CAE session" in payload(refused)["error"]["message"]
            closing = await call_ok(session, "submit_job", {"job_name": job_name, "close_open_odb": True,
                                                            "timeout": 600})
            assert closing["closedOdbs"] == [key] and closing["state"] == "COMPLETED", closing
            assert (await call_ok(session, "list_open_odbs"))["odbs"] == []
            await call_ok(session, "run_python", {"code": f"mcp_test_odb = odbAccess.openOdb({odb_path!r})\nresult = 1"})
            unmatched = await call_ok(session, "close_odbs", {"pattern": "*\\no-such-mcp.odb"})
            assert unmatched["closed"] == [] and [item["key"] for item in unmatched["remaining"]] == [key]
            closed = await call_ok(session, "close_odbs", {"pattern": f"*\\{job_name.upper()}.ODB"})
            assert [item["key"] for item in closed["closed"]] == [key] and closed["remaining"] == []

            image = await session.call_tool("capture_viewport", {})
            image_data = payload(image)
            assert not image.is_error and "imageBase64" not in image_data
            assert image_data["format"] == "png" and image_data["width"] > 0
            block = image.content[1]
            assert block.type == "image" and block.mime_type == "image/png"
            assert base64.b64decode(block.data)[:8] == PNG_SIGNATURE
            saved_path = RUN_ROOT / "viewport.png"
            saved = await call_ok(session, "capture_viewport", {"save_path": str(saved_path)})
            assert Path(saved["savedPath"]) == saved_path and saved_path.read_bytes()[:8] == PNG_SIGNATURE
            svg = await session.call_tool("capture_viewport", {"image_format": "SVG"})
            assert svg.is_error and "requires save_path" in payload(svg)["error"]["message"]
            data_uri = await session.call_tool("get_viewport_image", {})
            assert not data_uri.is_error and data_uri.content[0].text.startswith("data:image/png;base64,")
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})
            await call_ok(session, "set_workdir", {"path": original})


@pytest.mark.anyio
async def test_job_warnings_are_grouped():
    model_name, job_name = "McpTestLoose", "McpTestLooseJob"
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    async with mcp_session() as session:
        original = await _current_workdir(session)
        await call_ok(session, "set_workdir", {"path": str(RUN_ROOT)})
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL + LOOSE_BLOCK, model_name, job_name)})
            submitted = await call_ok(session, "submit_job", {"job_name": job_name, "timeout": 600})
            status = submitted["status"]
            assert submitted["state"] == "COMPLETED", submitted
            groups = {group["key"]: group for group in status["warnings"]}
            singular = next(group for key, group in groups.items() if "NUMERICAL SINGULARITY" in key)
            assert singular["count"] > 1 and singular["counts"]["msg"] == singular["count"]
            assert singular["firstFile"] == "msg" and singular["firstStep"] == 1 and singular["firstIncrement"] == 1
            assert "LOOSE-1." in singular["text"] and "\n" in singular["text"]
            assert any("UNCONNECTED REGIONS" in key for key in groups)
            assert status["warningCount"] == sum(group["count"] for group in status["warnings"])
            assert status["warningGroupCount"] == len(status["warnings"])

            detail = await call_ok(session, "monitor_job_status", {
                "job_name": job_name, "directory": str(RUN_ROOT), "warning_detail": singular["key"]})
            assert detail["detail"]["found"] is True and detail["detail"]["total"] == singular["count"]
            assert len(detail["detail"]["occurrences"]) == singular["count"]
            assert detail["state"] == "COMPLETED" and detail["caeStatus"] is None
            assert detail["source"] == "terminal marker in the latest job files"
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})
            await call_ok(session, "set_workdir", {"path": original})


@pytest.mark.anyio
async def test_submit_without_wait_monitor_conditions_and_terminate():
    model_name, job_name = "McpTestLong", "McpTestLongJob"
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    other = _case_dir("other-folder")
    async with mcp_session() as session:
        original = await _current_workdir(session)
        await call_ok(session, "set_workdir", {"path": str(RUN_ROOT)})
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name, long_run=True)})
            started = time.monotonic()
            submitted = await call_ok(session, "submit_job", {"job_name": job_name, "wait": False})
            assert time.monotonic() - started < 40
            assert submitted["completed"] is False and submitted["status"]["terminal"] is False, submitted
            assert "monitor_job_status" in submitted["note"]
            since = submitted["submittedAt"]

            running = await call_ok(session, "monitor_job_status", {
                "job_name": job_name, "since": since, "wait_seconds": 180, "until": "step>=1"})
            assert running["conditionMet"] is True and running["state"] == "RUNNING", running
            changed = await call_ok(session, "monitor_job_status", {
                "job_name": job_name, "since": since, "wait_seconds": 60, "until": "changed"})
            assert changed["conditionMet"] is True and changed["state"] == "RUNNING", changed
            again = await session.call_tool("submit_job", {"job_name": job_name, "timeout": 5})
            assert again.is_error and "is already RUNNING" in payload(again)["error"]["message"]

            # 按目录查看时不使用 CAE 的作业状态，由求解进程判断；其他目录中的同名作业不受影响
            folder = await call_ok(session, "monitor_job_status", {"job_name": job_name, "directory": str(RUN_ROOT)})
            assert folder["state"] == "RUNNING" and folder["source"] == "solver process", folder
            assert folder["caeStatus"] is None and folder["processes"]
            assert all(any(Path(path) == RUN_ROOT for path in item["directories"]) for item in folder["processes"])
            elsewhere = await call_ok(session, "monitor_job_status", {"job_name": job_name, "directory": str(other)})
            assert elsewhere["state"] == "UNKNOWN" and elsewhere["processes"] == [], elsewhere

            terminated = await call_ok(session, "monitor_job_status",
                                       {"job_name": job_name, "since": since, "terminate": True})
            termination = terminated["termination"]
            assert termination["requested"] is True and termination["returnCode"] == 0, termination
            assert "Sent Terminate message" in termination["output"]

            final = await call_ok(session, "monitor_job_status", {"job_name": job_name, "since": since,
                                                                  "wait_seconds": 300})
            assert final["conditionMet"] is True and final["until"] == "terminal", final
            assert final["state"] == "TERMINATED" and final["fileState"] == "TERMINATED", final
            assert final["files"]["lck"]["exists"] is False and final["processes"] == []
            from_files = await call_ok(session, "monitor_job_status", {"job_name": job_name,
                                                                       "directory": str(RUN_ROOT)})
            assert from_files["state"] == "TERMINATED" and from_files["source"] == "terminal marker in the latest job files"
            repeated = await call_ok(session, "monitor_job_status",
                                     {"job_name": job_name, "since": since, "terminate": True})
            assert repeated["termination"]["requested"] is False and repeated["termination"]["confirmed"] is True
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})
            await call_ok(session, "set_workdir", {"path": original})


@pytest.mark.anyio
async def test_timed_out_running_call_can_be_followed():
    # 内核执行代码期间插件无法回复；服务端等待 timeout + 10 秒后返回 UNKNOWN，代码运行 13 秒后结束，结果由 get_execution 取回
    async with mcp_session() as session:
        started = time.monotonic()
        result = await session.call_tool("run_python", {
            "code": "import time\ntime.sleep(13)\nresult = 'finished late'", "timeout": 1})
        waited = time.monotonic() - started
        data = payload(result)
        assert result.is_error and data["state"] == "UNKNOWN", data
        assert data["error"]["type"] == "TimeoutError" and "get_execution" in data["error"]["hint"]
        assert 10 <= waited < 13, waited
        followed = await call_ok(session, "get_execution", {"execution_id": data["executionId"], "wait_seconds": 30})
        assert followed["state"] == "COMPLETED" and followed["abandoned"] is True, followed
        assert followed["result"]["ok"] is True and followed["result"]["value"] == "finished late"
        assert time.monotonic() - started >= 13
        again = await call_ok(session, "get_execution", {"execution_id": data["executionId"]})
        assert again["delivered"] is True and again["result"]["value"] == "finished late"
        unknown = await session.call_tool("get_execution", {"execution_id": str(uuid.uuid4())})
        assert unknown.is_error and "Unknown executionId" in payload(unknown)["error"]["message"]


@pytest.mark.anyio
async def test_queued_call_is_cancelled_after_timeout():
    async with mcp_session() as first, mcp_session() as second:
        await call_ok(first, "run_python", {"code": "globals().pop('mcp_cancel_marker', None)\nresult = 'ready'",
                                            "namespace": SHARED_NAMESPACE})
        outcomes = {}

        async def hold_gui() -> None:
            outcomes["hold"] = await first.call_tool("run_python", {
                "code": "import time\ntime.sleep(10)\nresult = 'held'", "timeout": 60})

        async with anyio.create_task_group() as group:
            group.start_soon(hold_gui)
            await anyio.sleep(2)
            outcomes["queued"] = await second.call_tool("run_python", {
                "code": "mcp_cancel_marker = True", "namespace": SHARED_NAMESPACE, "timeout": 1})
        held = payload(outcomes["hold"])
        queued = payload(outcomes["queued"])
        assert not outcomes["hold"].is_error and held["value"] == "held"
        assert outcomes["queued"].is_error and queued["state"] == "CANCELLED", queued
        assert "cancelled before it started" in queued["error"]["hint"]
        assert (await call_ok(first, "get_execution", {"execution_id": queued["executionId"]}))["state"] == "CANCELLED"
        assert (await call_ok(first, "run_python", {"code": "'mcp_cancel_marker' in dir()",
                                                    "namespace": SHARED_NAMESPACE}))["value"] is False


@pytest.mark.anyio
async def test_resources_report_live_telemetry():
    async with mcp_session() as session:
        telemetry = await session.read_resource("abaqus://session-telemetry")
        status = await session.read_resource("abaqus://status")
        instructions = await session.read_resource("abaqus://agent-instructions")
    telemetry_data = json.loads(telemetry.contents[0].text)
    assert telemetry_data["connected"] is True
    assert telemetry_data["session"]["bridge"]["version"] == abaqus_mcp.__version__
    assert json.loads(status.contents[0].text)["connected"] is True
    assert instructions.contents[0].text == server.INSTRUCTIONS


def test_cli_check_doctor_and_setup():
    check = _run_cli("abaqus-mcp-check")
    assert check.returncode == 0, check.stderr
    assert "Abaqus MCP bridge is reachable." in check.stdout
    doctor = _run_cli("abaqus-mcp-doctor", "--verify-connection")
    assert doctor.returncode == 0, doctor.stderr
    assert f'"version": "{abaqus_mcp.__version__}"' in doctor.stdout and '"matchesPackage": true' in doctor.stdout
    target_dir = RUN_ROOT / f"plugin-setup-{uuid.uuid4().hex}"
    env = {**os.environ, "ABAQUS_MCP_PLUGIN_DIR": str(target_dir)}
    first = _run_cli("abaqus-mcp-setup", env=env)
    second = _run_cli("abaqus-mcp-setup", env=env)
    assert first.returncode == 0 and "Installed Abaqus/CAE plugin" in first.stdout, first.stderr
    assert second.returncode == 0 and "Plugin already up to date" in second.stdout, second.stderr
    installed = (target_dir / "abaqus_mcp_plugin.py").read_bytes()
    assert installed == (PROJECT_ROOT / "abaqus_mcp" / "gui_plugin.py").read_bytes()


def test_python2_abaqus_gets_a_clear_error():
    assert PYTHON2_LAUNCHER.is_file(), f"this check runs the plugin with Abaqus 2021 (Python 2) at {PYTHON2_LAUNCHER}"
    completed = subprocess.run(["cmd", "/c", str(PYTHON2_LAUNCHER), "python",
                                str(PROJECT_ROOT / "abaqus_mcp" / "gui_plugin.py")],
                               capture_output=True, text=True, errors="replace", timeout=300)
    # Abaqus 启动脚本不传递 Python 的退出码，只能根据输出判断
    output = completed.stdout + completed.stderr
    assert "RuntimeError: Abaqus MCP requires Abaqus/CAE with Python 3" in output, output

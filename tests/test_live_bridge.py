import ast
import base64
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import anyio
import pytest

import abaqus_mcp
from abaqus_mcp import server
from support import (BUILD_MODEL, PROJECT_ROOT, REMOVE_MODEL, RUN_ROOT, SCRIPTS_DIR, call_ok, mcp_session, model_code,
                     payload)

EXPECTED_TOOLS = {
    "ping", "check_abaqus_connection", "run_python", "execute_script", "set_workdir", "get_model_info", "list_jobs",
    "submit_job", "monitor_job_status", "inspect_odb", "get_odb_info", "capture_viewport", "get_viewport_image",
}
EXPECTED_RESOURCES = {"abaqus://session-telemetry", "abaqus://status", "abaqus://agent-instructions"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PYTHON2_LAUNCHER = Path(r"C:\SIMULIA\Commands2021\abq2021.bat")


def _run_cli(name: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPTS_DIR / f"{name}.exe"), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=300)


async def _current_workdir(session) -> str:
    return (await call_ok(session, "run_python", {"code": "import os\nresult = os.getcwd()"}))["value"]


@pytest.mark.anyio
async def test_tools_and_resources_are_listed():
    async with mcp_session() as session:
        tools = (await session.list_tools()).tools
        resources = (await session.list_resources()).resources
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert "timeout" in tool.input_schema["properties"], tool.name
    assert {str(resource.uri) for resource in resources} == EXPECTED_RESOURCES


def test_plugin_version_matches_package():
    tree = ast.parse((PROJECT_ROOT / "abaqus_mcp" / "gui_plugin.py").read_text(encoding="utf-8"))
    versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    assert versions == [abaqus_mcp.__version__]


@pytest.mark.anyio
async def test_arguments_are_validated_before_reaching_abaqus():
    cases = [
        ("run_python", {"code": "   "}, "code must not be empty"),
        ("run_python", {"code": "1", "timeout": 0}, "timeout must be"),
        ("ping", {"timeout": -1}, "timeout must be"),
        ("execute_script", {"script": ""}, "script must not be empty"),
        ("set_workdir", {"path": " "}, "path must not be empty"),
        ("submit_job", {"job_name": ""}, "job_name must not be empty"),
        ("monitor_job_status", {"terminate": True}, "job_name is required"),
        ("monitor_job_status", {"job_name": "x", "since": -5}, "since must be"),
        ("inspect_odb", {"odb_path": ""}, "odb_path must not be empty"),
        ("inspect_odb", {"odb_path": "a.odb", "max_points": 1}, "max_points must be"),
        ("get_odb_info", {"odb_path": ""}, "odb_path must not be empty"),
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
    assert bridge["version"] == abaqus_mcp.__version__ and bridge["port"] == 48152 and bridge["running"] is True
    assert isinstance(bridge["processed"], int) and bridge["gui_thread"] == "MainThread"
    assert not text.is_error
    assert text.content[0].text.startswith(
        f"Connected to Abaqus/CAE {info['abaqus_version']} through the MCP bridge at 127.0.0.1:48152 "
        f"(bridge {abaqus_mcp.__version__}).")


@pytest.mark.anyio
async def test_run_python_returns_values_and_clears_result():
    async with mcp_session() as session:
        try:
            assert (await call_ok(session, "run_python", {"code": "1 + 1"}))["value"] == 2
            assert (await call_ok(session, "run_python", {"code": "result = {'a': 1}"}))["value"] == {"a": 1}
            assert (await call_ok(session, "run_python", {"code": "mcp_test_value = 21"}))["value"] is None
            assert (await call_ok(session, "run_python", {"code": "mcp_test_value * 2"}))["value"] == 42
            printed = await call_ok(session, "run_python", {"code": "print('hello from abaqus')\nresult = 3"})
            assert printed["value"] == 3 and printed["stdout"] == "hello from abaqus\n"
        finally:
            await call_ok(session, "run_python", {"code": "globals().pop('mcp_test_value', None)\nresult = 'cleaned'"})


@pytest.mark.anyio
async def test_run_python_errors_carry_location_and_suggestions():
    async with mcp_session() as session:
        missing = await session.call_tool("run_python", {"code": "x = 1\nmodel = mdb.models['NoSuchModel']"})
        typo = await session.call_tool("run_python", {"code": "mdbb.models.keys()"})
    missing_data = payload(missing)
    assert missing.is_error and missing_data["ok"] is False
    assert missing_data["error"]["type"] == "KeyError" and missing_data["error"]["at"]["line"] == 2
    suggestions = missing_data["error"]["suggestions"]
    assert suggestions["missing_key"] == "NoSuchModel"
    assert suggestions.get("possible_keys", []) + suggestions.get("available_keys_sample", [])
    typo_data = payload(typo)
    assert typo.is_error and typo_data["error"]["type"] == "NameError"
    assert "mdb" in typo_data["error"]["suggestions"]["possible_names"]


@pytest.mark.anyio
async def test_execute_script_returns_text():
    async with mcp_session() as session:
        printed = await session.call_tool("execute_script", {"script": "print('line')\nresult = {'k': 1}"})
        quiet = await session.call_tool("execute_script", {"script": "mcp_quiet_value = 1"})
        failed = await session.call_tool("execute_script", {"script": "1 / 0"})
        await call_ok(session, "run_python", {"code": "globals().pop('mcp_quiet_value', None)\nresult = 'cleaned'"})
    assert not printed.is_error and printed.content[0].text == 'line\n\n{\n  "k": 1\n}'
    assert not quiet.is_error and quiet.content[0].text == "(Script executed successfully, no output)"
    assert failed.is_error and payload(failed)["error"]["type"] == "ZeroDivisionError"


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

            submitted = await call_ok(session, "submit_job", {"job_name": job_name, "timeout": 600})
            assert submitted["completed"] is True and submitted["state"] == "COMPLETED", submitted
            assert submitted["status"]["fileState"] == "COMPLETED" and submitted["status"]["source"] == "CAE job status"

            monitored = await call_ok(session, "monitor_job_status",
                                      {"job_name": job_name, "since": submitted["submittedAt"]})
            assert monitored["state"] == "COMPLETED" and monitored["terminal"] is True
            assert monitored["runFreshness"] == "confirmed by CAE" and monitored["files"]["lck"]["exists"] is False
            assert monitored["errors"] == []
            listing = await call_ok(session, "monitor_job_status")
            assert next(job for job in listing["jobs"] if job["name"] == job_name)["caeStatus"] == "COMPLETED"

            odb_path = str(RUN_ROOT / f"{job_name}.odb")
            meta = await call_ok(session, "inspect_odb", {"odb_path": odb_path})
            assert [step["name"] for step in meta["steps"]] == ["Load"] and meta["steps"][0]["duration"] == 1.0
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
async def test_submit_wait_limit_and_terminate():
    model_name, job_name = "McpTestLong", "McpTestLongJob"
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    async with mcp_session() as session:
        original = await _current_workdir(session)
        await call_ok(session, "set_workdir", {"path": str(RUN_ROOT)})
        try:
            await call_ok(session, "run_python", {"code": model_code(BUILD_MODEL, model_name, job_name, long_run=True)})
            submitted = await call_ok(session, "submit_job", {"job_name": job_name, "timeout": 20})
            assert submitted["completed"] is False and submitted["status"]["terminal"] is False, submitted
            assert "monitor_job_status" in submitted["note"]
            since = submitted["submittedAt"]

            deadline = time.monotonic() + 180
            while True:
                running = await call_ok(session, "monitor_job_status", {"job_name": job_name, "since": since})
                if running["state"] == "RUNNING" and (running["increment"] or 0) >= 1:
                    break
                assert time.monotonic() < deadline, running
                await anyio.sleep(3)
            again = await session.call_tool("submit_job", {"job_name": job_name, "timeout": 5})
            assert again.is_error and "is already RUNNING" in payload(again)["error"]["message"]

            terminated = await call_ok(session, "monitor_job_status",
                                       {"job_name": job_name, "since": since, "terminate": True})
            termination = terminated["termination"]
            assert termination["requested"] is True and termination["returnCode"] == 0, termination
            assert "Sent Terminate message" in termination["output"]

            deadline = time.monotonic() + 300
            while True:
                final = await call_ok(session, "monitor_job_status", {"job_name": job_name, "since": since})
                if final["terminal"]:
                    break
                assert time.monotonic() < deadline, final
                await anyio.sleep(5)
            assert final["state"] == "TERMINATED" and final["fileState"] == "TERMINATED", final
            assert final["files"]["lck"]["exists"] is False
            repeated = await call_ok(session, "monitor_job_status",
                                     {"job_name": job_name, "since": since, "terminate": True})
            assert repeated["termination"]["requested"] is False and repeated["termination"]["confirmed"] is True
        finally:
            await call_ok(session, "run_python", {"code": model_code(REMOVE_MODEL, model_name, job_name)})
            await call_ok(session, "set_workdir", {"path": original})


@pytest.mark.anyio
async def test_timed_out_running_call_has_unknown_outcome():
    # 服务端最多等待 timeout + 10 秒；代码运行 13 秒，服务端先放弃等待，结果应为 UNKNOWN，而代码仍会执行完
    async with mcp_session() as session:
        await call_ok(session, "run_python", {"code": "globals().pop('mcp_unknown_marker', None)\nresult = 'ready'"})
        started = time.monotonic()
        result = await session.call_tool("run_python", {
            "code": "import time\ntime.sleep(13)\nmcp_unknown_marker = True", "timeout": 1})
        waited = time.monotonic() - started
        data = payload(result)
        assert result.is_error and data["state"] == "UNKNOWN", data
        assert "TimeoutError" in data["error"]["type"] and "unknown outcome" in data["error"]["hint"]
        assert 10 <= waited < 13, waited
        deadline = time.monotonic() + 60
        while not (await call_ok(session, "run_python", {"code": "'mcp_unknown_marker' in globals()"}))["value"]:
            assert time.monotonic() < deadline
            await anyio.sleep(1)
        assert time.monotonic() - started >= 13
        await call_ok(session, "run_python", {"code": "globals().pop('mcp_unknown_marker', None)\nresult = 'cleaned'"})


@pytest.mark.anyio
async def test_queued_call_is_cancelled_after_timeout():
    async with mcp_session() as first, mcp_session() as second:
        await call_ok(first, "run_python", {"code": "globals().pop('mcp_cancel_marker', None)\nresult = 'ready'"})
        outcomes = {}

        async def hold_gui() -> None:
            outcomes["hold"] = await first.call_tool("run_python", {
                "code": "import time\ntime.sleep(10)\nresult = 'held'", "timeout": 60})

        async with anyio.create_task_group() as group:
            group.start_soon(hold_gui)
            await anyio.sleep(2)
            outcomes["queued"] = await second.call_tool("run_python", {"code": "mcp_cancel_marker = True", "timeout": 1})
        held = payload(outcomes["hold"])
        queued = payload(outcomes["queued"])
        assert not outcomes["hold"].is_error and held["value"] == "held"
        assert outcomes["queued"].is_error and queued["state"] == "CANCELLED", queued
        assert "cancelled before it started" in queued["error"]["hint"]
        assert (await call_ok(first, "run_python", {"code": "'mcp_cancel_marker' in globals()"}))["value"] is False


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

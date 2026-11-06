import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psutil

from . import kernel_code
from .kernel_wrapper import PROTOCOL, WRAPPER
from .protocol import max_message_bytes, read_message, send_message

START_HINT = "Choose Plug-ins > Abaqus MCP > Start MCP Bridge in Abaqus/CAE, or set ABAQUS_MCP_AUTOSTART=1 for Abaqus/CAE."
NO_CAE_HINT = "Open Abaqus/CAE, then choose Plug-ins > Abaqus MCP > Start MCP Bridge."
BUSY_HINT = ("Abaqus/CAE did not answer in time. A call that was already running has an unknown outcome; follow it "
             "with get_execution using this executionId, or inspect the model state, before repeating it.")
RUNNING_HINT = ("The code is still running in Abaqus/CAE. Call get_execution with this executionId (wait_seconds to "
                "wait) to follow it and fetch its result; do not repeat a modelling command.")
CANCELLED_HINT = "Abaqus/CAE was busy and the request was cancelled before it started; it can be sent again."
OUTDATED_HINT = ("The Abaqus/CAE plugin does not match this server. Copy abaqus_mcp_plugin.py from the same release into "
                 "%USERPROFILE%\\abaqus_plugins (or run abaqus-mcp-setup), then restart Abaqus/CAE.")
QUEUE_MARGIN_SECONDS = 5.0
READ_MARGIN_SECONDS = 10.0
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
CAE_GUI_PROCESS = "abqcaeg.exe"


class BridgeError(RuntimeError):
    def __init__(self, message: str, execution_id: str, state: str = "UNKNOWN",
                 error_type: str = "BridgeError", hint: str | None = None):
        super().__init__(message)
        self.execution_id = execution_id
        self.state = state
        self.error_type = error_type
        self.hint = hint


def cae_gui_processes() -> list[int]:
    return [process.info["pid"] for process in psutil.process_iter(["pid", "name"])
            if (process.info["name"] or "").lower() == CAE_GUI_PROCESS]


@dataclass(frozen=True)
class AbaqusBridgeClient:
    host: str
    port: int
    timeout: float

    @classmethod
    def from_environment(cls) -> "AbaqusBridgeClient":
        return cls(
            host=os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("ABAQUS_MCP_PORT", "48152")),
            timeout=float(os.environ.get("ABAQUS_MCP_TIMEOUT", "120")),
        )

    @property
    def is_local(self) -> bool:
        return self.host.lower() in LOCAL_HOSTS

    def _refused(self, exc: ConnectionRefusedError, execution_id: str) -> BridgeError:
        if not self.is_local:
            return BridgeError(str(exc), execution_id, "NOT_STARTED", "ConnectionRefusedError", NO_CAE_HINT)
        pids = cae_gui_processes()
        if pids:
            return BridgeError(f"Abaqus/CAE is running (GUI process {', '.join(map(str, pids))}) but the MCP bridge "
                               f"is not listening on {self.host}:{self.port}: {exc}", execution_id, "NOT_STARTED",
                               "ConnectionRefusedError", START_HINT)
        return BridgeError(f"Abaqus/CAE is not running: {exc}", execution_id, "NOT_STARTED", "ConnectionRefusedError",
                           NO_CAE_HINT)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = self.timeout if timeout is None else timeout
        execution_id = str(uuid.uuid4())
        request_params = dict(params or {})
        request_params["protocol"] = PROTOCOL
        request_params["timeout"] = effective_timeout
        request_params["executionId"] = execution_id
        # 等待期限按发出时刻计算：CAE 执行内核命令期间插件可能晚很久才读到请求，超过期限的请求插件不会执行
        request_params["deadline"] = time.time() + effective_timeout + QUEUE_MARGIN_SECONDS
        payload = {"id": execution_id, "method": method, "params": request_params}
        try:
            with socket.create_connection((self.host, self.port), timeout=effective_timeout) as sock:
                # 插件在期限到达后还需片刻返回取消或仍在运行的说明，读取等待时间相应放宽
                sock.settimeout(effective_timeout + READ_MARGIN_SECONDS)
                send_message(sock, payload)
                response = read_message(sock, max_message_bytes())
        except ConnectionRefusedError as exc:
            raise self._refused(exc, execution_id) from exc
        except TimeoutError as exc:
            raise BridgeError(str(exc) or "timed out waiting for the Abaqus bridge", execution_id, "UNKNOWN",
                              "TimeoutError", BUSY_HINT) from exc
        except Exception as exc:
            raise BridgeError(str(exc), execution_id, "UNKNOWN", type(exc).__name__) from exc

        if response.get("id") != execution_id:
            raise BridgeError("Abaqus bridge returned a mismatched response id", execution_id)
        if not response.get("ok", False):
            error = response.get("error") or {}
            message = error.get("message") or "Abaqus bridge returned an error"
            state = error.get("state", "UNKNOWN")
            error_type = (error.get("type") or "BridgeError").rsplit(".", 1)[-1]
            if "unknown method" in message or "Protocol mismatch" in message:
                raise BridgeError(message, execution_id, "NOT_STARTED", "PluginMismatch", OUTDATED_HINT)
            hint = {"CANCELLED": CANCELLED_HINT, "RUNNING": RUNNING_HINT}.get(state)
            if state in ("CANCELLED", "RUNNING"):
                error_type = "TimeoutError"
            raise BridgeError(message, execution_id, state, error_type, hint)
        result = response.get("result")
        if not isinstance(result, dict):
            raise BridgeError("Abaqus bridge returned an invalid result envelope", execution_id)
        # execution 请求返回的是被查询调用的 executionId
        if method != "execution" and result.get("executionId") not in (None, execution_id):
            raise BridgeError("Abaqus bridge returned a mismatched executionId", execution_id)
        return {**result, "requestId": execution_id, "executionId": execution_id}

    def run(self, kernel_request: dict[str, Any], timeout: float | None = None,
            include_bridge: bool = False) -> dict[str, Any]:
        return self.request("run", {"wrapper": WRAPPER, "request": kernel_request, "include_bridge": include_bridge},
                            timeout)

    def execute(self, code: str, timeout: float | None = None, namespace: str | None = None) -> dict[str, Any]:
        return self.run({"mode": "code", "code": code, "namespace": namespace}, timeout)

    def ping(self, timeout: float | None = None, namespace: str | None = None) -> dict[str, Any]:
        execution = self.run({"mode": "code", "code": kernel_code.PING, "namespace": None,
                              "describe_namespace": namespace}, timeout, include_bridge=True)
        if not execution.get("ok"):
            raise BridgeError(f"ping failed in the Abaqus kernel: {execution.get('core_error')}",
                              execution["executionId"], "UNKNOWN", "PingError")
        session = dict(execution["return_value"])
        session["bridge"] = execution["bridge"]
        if "namespace" in execution:
            session["namespace"] = execution["namespace"]
        session["executionId"] = execution["executionId"]
        session["requestId"] = execution["requestId"]
        return session

    def execution(self, target: str, wait_seconds: float = 0.0, timeout: float | None = None) -> dict[str, Any]:
        effective = max(self.timeout if timeout is None else timeout, wait_seconds + 5.0)
        return self.request("execution", {"target": target, "wait_seconds": wait_seconds}, effective)

    def stop(self, timeout: float | None = None) -> dict[str, Any]:
        return self.request("stop", timeout=timeout)

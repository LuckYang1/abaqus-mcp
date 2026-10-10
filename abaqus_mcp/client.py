import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .protocol import max_message_bytes, read_message, send_message

CONNECT_HINT = "Open Abaqus/CAE and choose Plug-ins > Abaqus MCP > Start MCP Bridge."
BUSY_HINT = ("Abaqus/CAE did not answer in time. A call that was already running has an unknown outcome; "
             "inspect the model state before repeating it.")
CANCELLED_HINT = "Abaqus/CAE was busy and the request was cancelled before it started; it can be sent again."
QUEUE_MARGIN_SECONDS = 5.0
READ_MARGIN_SECONDS = 10.0


class BridgeError(RuntimeError):
    def __init__(self, message: str, execution_id: str, state: str = "UNKNOWN",
                 error_type: str = "BridgeError", hint: str | None = None):
        super().__init__(message)
        self.execution_id = execution_id
        self.state = state
        self.error_type = error_type
        self.hint = hint


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

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = self.timeout if timeout is None else timeout
        execution_id = str(uuid.uuid4())
        request_params = dict(params or {})
        request_params["timeout"] = effective_timeout
        request_params["executionId"] = execution_id
        # 等待期限按发出时刻计算：CAE 执行内核命令期间插件可能晚很久才读到请求，超过期限的请求插件不会执行
        request_params["deadline"] = time.time() + effective_timeout + QUEUE_MARGIN_SECONDS
        payload = {"id": execution_id, "method": method, "params": request_params}
        try:
            with socket.create_connection((self.host, self.port), timeout=effective_timeout) as sock:
                # 插件在期限到达后还需片刻返回取消或结果未知的说明，读取等待时间相应放宽
                sock.settimeout(effective_timeout + READ_MARGIN_SECONDS)
                send_message(sock, payload)
                response = read_message(sock, max_message_bytes())
        except ConnectionRefusedError as exc:
            raise BridgeError(str(exc), execution_id, "NOT_STARTED", type(exc).__name__, CONNECT_HINT) from exc
        except TimeoutError as exc:
            raise BridgeError(str(exc) or "timed out waiting for the Abaqus bridge", execution_id, "UNKNOWN",
                              type(exc).__name__, BUSY_HINT) from exc
        except Exception as exc:
            raise BridgeError(str(exc), execution_id, "UNKNOWN", type(exc).__name__) from exc

        if response.get("id") != execution_id:
            raise BridgeError("Abaqus bridge returned a mismatched response id", execution_id)
        if not response.get("ok", False):
            error = response.get("error") or {}
            message = error.get("message") or "Abaqus bridge returned an error"
            state = error.get("state", "UNKNOWN")
            if state == "CANCELLED":
                hint = CANCELLED_HINT
            elif "timed out" in message:
                hint = BUSY_HINT
            else:
                hint = None
            raise BridgeError(message, execution_id, state, error.get("type", "BridgeError"), hint)
        result = response.get("result")
        if not isinstance(result, dict):
            raise BridgeError("Abaqus bridge returned an invalid result envelope", execution_id)
        if result.get("executionId") not in (None, execution_id):
            raise BridgeError("Abaqus bridge returned a mismatched executionId", execution_id)
        return {**result, "requestId": execution_id, "executionId": execution_id}

    def ping(self, timeout: float | None = None) -> dict[str, Any]:
        return self.request("ping", timeout=timeout)

    def execute(self, code: str, timeout: float | None = None) -> dict[str, Any]:
        return self.request("execute", {"code": code}, timeout)

    def stop(self, timeout: float | None = None) -> dict[str, Any]:
        return self.request("stop", timeout=timeout)

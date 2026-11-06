# -*- coding: utf-8 -*-
# Abaqus/CAE 图形界面插件：安装为 abaqus_plugins/abaqus_mcp_plugin.py，在 CAE 内监听本地 socket，
# 在图形界面主线程上把服务端发来的包装代码交给 Abaqus 内核执行，并保存最近的执行记录。
# 模块级代码保持 Python 2 可解析，以便旧版 Abaqus 给出明确报错。
import sys

if sys.version_info[0] < 3:
    raise RuntimeError("Abaqus MCP requires Abaqus/CAE with Python 3 (Abaqus 2024 or later).")

from abaqusGui import (
    AFXForm,
    AFXMode,
    FXMAPFUNC,
    SEL_COMMAND,
    SEL_TIMEOUT,
    getAFXApp,
    sendCommand,
    showAFXErrorDialog,
)
import base64
import collections
import json
import os
import platform
import queue
import socketserver
import tempfile
import threading
import time
import traceback
import uuid

__version__ = "1.2.0"
PROTOCOL = 2

HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
MAX_MESSAGE_BYTES = int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(16 * 1024 * 1024)))
DEBUG_LOG = os.environ.get("ABAQUS_MCP_DEBUG", "").lower() in ("1", "true", "yes", "on")
AUTOSTART = os.environ.get("ABAQUS_MCP_AUTOSTART", "").lower() in ("1", "true", "yes", "on")
LOG_PATH = os.environ.get("ABAQUS_MCP_LOG") or os.path.join(tempfile.gettempdir(), "abaqus_mcp_plugin.log")
LOG_LIMIT = 1024 * 1024
POLL_MILLISECONDS = 50
BATCH_SIZE = 5
EXECUTION_LIMIT = 200
UNDELIVERED_RESULT_LIMIT = 20
TERMINAL_STATES = ("completed", "failed", "cancelled")
MENU_MODULES = ["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"]

_LOG_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_EXECUTIONS_LOCK = threading.Lock()
_REQUESTS = queue.Queue()
_STOP_REQUESTED = threading.Event()
_STATS = {"processed": 0, "started_at": None}
_EXECUTIONS = collections.OrderedDict()
_SERVER = None
_DISPATCHER = None


def _log(message):
    if not DEBUG_LOG:
        return
    with _LOG_LOCK:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= LOG_LIMIT:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))


def _clear_debug_log():
    # 未开启调试日志时，清除以前开启调试时留下的日志
    if DEBUG_LOG:
        return
    for path in (LOG_PATH, LOG_PATH + ".1"):
        if os.path.isfile(path):
            os.remove(path)


def _announce(message):
    getAFXApp().getAFXMainWindow().writeToMessageArea(message)


def _send(sock, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def _recv(sock):
    chunks = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        if newline >= 0:
            total += newline
            if total > MAX_MESSAGE_BYTES:
                raise RuntimeError("request exceeded %d bytes" % MAX_MESSAGE_BYTES)
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MESSAGE_BYTES:
            raise RuntimeError("request exceeded %d bytes" % MAX_MESSAGE_BYTES)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Execution:
    # 一次执行从排队到结束的状态。图形界面主线程取出时判定是否已超过期限；
    # 调用方不再等待（abandoned）后，内核写出的结果仍会被收集，供 get_execution 取回。
    # 处理请求的线程与图形界面主线程共同修改状态，由锁保护
    def __init__(self, execution_id, params):
        self.id = execution_id
        self.params = params
        self.deadline = float(params["deadline"])
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = "queued"
        self.submitted_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.response_path = None
        self.result = None
        self.error = None
        self.abandoned = False
        self.delivered = False
        self.gui_thread = None

    def claim(self):
        # CAE 执行内核命令期间可能暂停插件的后台线程，取消不能只依赖处理请求的线程；
        # 期限由服务端按发出时刻计算，插件晚读到请求也不会影响判定
        with self.lock:
            if self.state != "queued":
                return False
            if time.time() >= self.deadline:
                self.state = "cancelled"
                self.finished_at = time.time()
                self.event.set()
                return False
            self.state = "running"
            self.started_at = time.time()
            return True

    def settle(self):
        # 处理请求的线程等待结束后调用：仍在排队则取消，仍在运行则记为调用方已放弃等待
        with self.lock:
            if self.state == "queued":
                self.state = "cancelled"
                self.finished_at = time.time()
                self.event.set()
            elif self.state == "running":
                self.abandoned = True
            return self.state

    def collect(self):
        # 图形界面主线程在内核命令结束后读取结果文件
        with self.lock:
            if self.state != "running" or self.response_path is None or not os.path.exists(self.response_path):
                return False
            with open(self.response_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            os.remove(self.response_path)
            result = payload["result"]
            if result.get("executionId") != self.id:
                raise RuntimeError("kernel response executionId mismatch")
            self.result = result
            self.state = "completed" if result.get("ok") else "failed"
            self.finished_at = time.time()
            self.event.set()
        _trim_undelivered()
        return True

    def fail(self, exc):
        with self.lock:
            self.error = exc
            self.state = "failed"
            self.finished_at = time.time()
            self.event.set()

    def summary(self):
        with self.lock:
            data = {
                "executionId": self.id,
                "state": self.state.upper(),
                "submittedAt": self.submitted_at,
                "startedAt": self.started_at,
                "finishedAt": self.finished_at,
                "abandoned": self.abandoned,
                "delivered": self.delivered,
            }
            if self.result is not None:
                data["result"] = self.result
            elif self.error is not None:
                data["bridgeError"] = {"type": type(self.error).__name__, "message": str(self.error)}
            elif self.state in ("completed", "failed") and self.delivered:
                data["note"] = "The result was already returned to the original call."
            elif self.state in ("completed", "failed"):
                data["note"] = ("The result was discarded; the bridge keeps the results of the latest %d calls whose "
                                "caller stopped waiting." % UNDELIVERED_RESULT_LIMIT)
            return data


def _register(execution):
    with _EXECUTIONS_LOCK:
        _EXECUTIONS[execution.id] = execution
        while len(_EXECUTIONS) > EXECUTION_LIMIT:
            oldest = next(iter(_EXECUTIONS))
            if _EXECUTIONS[oldest].state not in TERMINAL_STATES:
                break
            _EXECUTIONS.pop(oldest)


def _trim_undelivered():
    with _EXECUTIONS_LOCK:
        kept = [item for item in _EXECUTIONS.values() if item.result is not None]
    for item in kept[:-UNDELIVERED_RESULT_LIMIT]:
        with item.lock:
            item.result = None


def _kernel_command(wrapper, request):
    return ("exec(compile(__import__('base64').b64decode(%r).decode('utf-8'), '<abaqus-mcp-wrapper>', 'exec'), "
            "{'__name__': '__abaqus_mcp_wrapper__', '_kernel_globals': globals(), "
            "'_request': __import__('json').loads(__import__('base64').b64decode(%r).decode('utf-8'))})"
            % (_b64(wrapper), _b64(json.dumps(request, ensure_ascii=False))))


def _run_on_gui_thread(execution):
    params = execution.params
    request = dict(params["request"])
    execution.response_path = os.path.join(tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex)
    request["execution_id"] = execution.id
    request["response_path"] = execution.response_path
    # 图形界面进程的当前目录保持为 CAE 的启动目录，abaqus.rpy 写在这里
    request["gui_cwd"] = os.getcwd()
    execution.gui_thread = threading.current_thread().name
    _log("sendCommand start id=%s" % execution.id)
    # sendCommand 在内核执行完命令后才返回，期间占用 Python 解释器锁，插件的后台线程都无法运行；
    # 包装代码在命令结束前写出结果文件，返回后文件必然存在
    sendCommand(_kernel_command(params["wrapper"], request), False, False)
    if not execution.collect():
        raise RuntimeError("the kernel command returned without writing a response to %s" % execution.response_path)
    _log("kernel response id=%s state=%s" % (execution.id, execution.state))


def _bridge_status():
    started_at = _STATS["started_at"]
    return {
        "version": __version__,
        "protocol": PROTOCOL,
        "host": HOST,
        "port": PORT,
        "transport": "socket",
        "running": _SERVER is not None,
        "processed": _STATS["processed"],
        "queued": _REQUESTS.qsize(),
        "uptime_seconds": int(time.time() - started_at) if started_at is not None and _SERVER is not None else 0,
        "debug_log": LOG_PATH if DEBUG_LOG else None,
        "autostart": AUTOSTART,
    }


def _check_protocol(params):
    protocol = params.get("protocol")
    if protocol != PROTOCOL:
        raise RuntimeError("Protocol mismatch: the Abaqus MCP plugin %s speaks protocol %d but the server sent %r. "
                           "Install the plugin that matches the server and restart Abaqus/CAE."
                           % (__version__, PROTOCOL, protocol))


class McpGuiHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request_id = None
        try:
            message = _recv(self.request)
            request_id = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            _log("request method=%s id=%s" % (method, request_id))
            if method == "stop":
                # 先回复，再由图形界面主线程的轮询回调执行停止：只有主线程运行 Python 代码时，
                # CAE 才给插件的后台线程运行机会，停止监听必须在轮询仍在进行时由主线程完成
                _send(self.request, {"id": request_id, "ok": True, "result": {"stopping": True, "executionId": params.get("executionId")}})
                _STOP_REQUESTED.set()
                return
            if method == "run":
                _check_protocol(params)
                result = self._run(params)
            elif method == "execution":
                _check_protocol(params)
                result = self._execution(params)
            else:
                raise ValueError("unknown method: %r; the Abaqus MCP plugin %s accepts run, execution and stop "
                                 "(protocol %d). Use the server that matches this plugin." % (method, __version__, PROTOCOL))
            _send(self.request, {"id": request_id, "ok": True, "result": result})
            _log("response ok id=%s" % request_id)
        except Exception as exc:
            _log("response error id=%s type=%s" % (request_id, type(exc).__name__))
            state = getattr(exc, "state", "UNKNOWN")
            _send(self.request, {
                "id": request_id,
                "ok": False,
                "error": {
                    "message": str(exc),
                    "type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                    "state": state,
                },
            })

    def _run(self, params):
        if _DISPATCHER is None:
            raise RuntimeError("GUI dispatcher is not initialized")
        execution = Execution(params["executionId"], params)
        _register(execution)
        _REQUESTS.put(execution)
        execution.event.wait(max(0.0, execution.deadline - time.time()))
        state = execution.settle()
        if state == "cancelled":
            raise _StateError("CANCELLED", "timed out before GUI execution; request cancelled")
        if state == "running":
            raise _StateError("RUNNING", "timed out while the Abaqus kernel was still running the code")
        if execution.error is not None:
            raise execution.error
        with execution.lock:
            result = execution.result
            execution.result = None
            execution.delivered = True
        if params.get("include_bridge"):
            bridge = _bridge_status()
            bridge.update({
                "gui_python": sys.version,
                "gui_platform": platform.platform(),
                "gui_thread": execution.gui_thread,
                "gui_cwd": os.getcwd(),
            })
            result["bridge"] = bridge
        return result

    def _execution(self, params):
        target = params.get("target")
        with _EXECUTIONS_LOCK:
            execution = _EXECUTIONS.get(target)
        if execution is None:
            raise KeyError("Unknown executionId %r; the bridge keeps the latest %d executions since it started"
                           % (target, EXECUTION_LIMIT))
        # 内核正在执行命令时本线程得不到运行机会，这里的等待只在命令之间进行
        end = min(time.time() + float(params.get("wait_seconds") or 0.0), float(params["deadline"]) - 1.0)
        execution.event.wait(max(0.0, end - time.time()))
        summary = execution.summary()
        if "result" in summary:
            with execution.lock:
                execution.delivered = True
        return summary


class _StateError(RuntimeError):
    def __init__(self, state, message):
        RuntimeError.__init__(self, message)
        self.state = state


class McpGuiServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_bridge():
    global _SERVER
    with _STATE_LOCK:
        if _SERVER is not None:
            return False
        _clear_debug_log()
        _STOP_REQUESTED.clear()
        server = McpGuiServer((HOST, PORT), McpGuiHandler)
        threading.Thread(target=server.serve_forever, name="AbaqusMcpBridge", daemon=True).start()
        _SERVER = server
        _STATS["processed"] = 0
        _STATS["started_at"] = time.time()
    _log("bridge started on %s:%s" % (HOST, PORT))
    return True


def stop_bridge():
    global _SERVER
    with _STATE_LOCK:
        server = _SERVER
        _SERVER = None
    if server is None:
        return False
    server.shutdown()
    server.server_close()
    _log("bridge stopped")
    return True


class McpGuiActionForm(AFXForm):
    ID_POLL = AFXForm.ID_LAST + 1

    def __init__(self, owner, action):
        global _DISPATCHER
        AFXForm.__init__(self, owner)
        self.action = action
        FXMAPFUNC(self, SEL_COMMAND, AFXMode.ID_ACTIVATE, McpGuiActionForm.onCmdActivate)
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_POLL, McpGuiActionForm.onTimeout)
        _DISPATCHER = self

    def getFirstDialog(self):
        return None

    def _schedule_poll(self):
        getAFXApp().addTimeout(POLL_MILLISECONDS, self, self.ID_POLL)

    def onCmdActivate(self, sender, sel, ptr):
        if self.action == "start":
            self.start(False)
        elif self.action == "stop":
            self._stop()
        else:
            self._status()
        return 1

    def start(self, automatic):
        try:
            started = start_bridge()
        except Exception as exc:
            _log("bridge start failed type=%s" % type(exc).__name__)
            message = "Abaqus MCP bridge failed to start: %s" % exc
            if automatic:
                # 自动启动时端口可能已被另一个 CAE 会话占用，只在消息区说明，不弹出窗口
                _announce(message)
            else:
                showAFXErrorDialog(getAFXApp().getAFXMainWindow(), message)
            return
        if not started:
            _announce("Abaqus MCP bridge is already running on %s:%s." % (HOST, PORT))
            return
        self._schedule_poll()
        _announce("Abaqus MCP %s bridge listening on %s:%s%s." % (
            __version__, HOST, PORT, " (started automatically)" if automatic else ""))
        if DEBUG_LOG:
            _announce("Abaqus MCP debug log: %s" % LOG_PATH)

    def _stop(self):
        if stop_bridge():
            _announce("Abaqus MCP bridge stopped.")
        else:
            _announce("Abaqus MCP bridge is not running.")

    def _status(self):
        _announce("Abaqus MCP bridge status:\n%s" % json.dumps(_bridge_status(), indent=2, ensure_ascii=False))

    def onTimeout(self, sender, sel, ptr):
        if _STOP_REQUESTED.is_set():
            _STOP_REQUESTED.clear()
            if stop_bridge():
                _announce("Abaqus MCP bridge stopped by a remote stop request.")
            return 1
        handled = 0
        while handled < BATCH_SIZE:
            try:
                execution = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if not execution.claim():
                continue
            try:
                _log("GUI thread running id=%s" % execution.id)
                _run_on_gui_thread(execution)
            except Exception as exc:
                execution.fail(exc)
                _log("GUI thread error id=%s type=%s" % (execution.id, type(exc).__name__))
            finally:
                _STATS["processed"] += 1
        if _SERVER is not None:
            self._schedule_poll()
        return 1


_toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
_start_form = McpGuiActionForm(_toolset, "start")
_toolset.registerGuiMenuButton(
    object=_start_form,
    buttonText="Abaqus MCP|Start MCP Bridge",
    version=__version__,
    applicableModules=MENU_MODULES,
    description="Start the local MCP bridge for the active Abaqus/CAE session.",
)
_toolset.registerGuiMenuButton(
    object=McpGuiActionForm(_toolset, "status"),
    buttonText="Abaqus MCP|Bridge Status",
    version=__version__,
    applicableModules=MENU_MODULES,
    description="Print the Abaqus MCP bridge status in the message area.",
)
_toolset.registerGuiMenuButton(
    object=McpGuiActionForm(_toolset, "stop"),
    buttonText="Abaqus MCP|Stop MCP Bridge",
    version=__version__,
    applicableModules=MENU_MODULES,
    description="Stop the local MCP bridge.",
)
if AUTOSTART:
    _announce("Abaqus MCP %s plugin loaded; ABAQUS_MCP_AUTOSTART is set, starting the bridge." % __version__)
    _start_form.start(True)
else:
    _announce("Abaqus MCP %s plugin loaded. Use Plug-ins > Abaqus MCP > Start MCP Bridge." % __version__)

# -*- coding: utf-8 -*-
# Abaqus/CAE 图形界面插件：安装为 abaqus_plugins/abaqus_mcp_plugin.py，在 CAE 内监听本地 socket，
# 在图形界面主线程上把请求交给 Abaqus 内核执行。模块级代码保持 Python 2 可解析，以便旧版 Abaqus 给出明确报错。
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

__version__ = "1.1.0"

HOST = os.environ.get("ABAQUS_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABAQUS_MCP_PORT", "48152"))
DEFAULT_TIMEOUT = float(os.environ.get("ABAQUS_MCP_TIMEOUT", "120"))
MAX_MESSAGE_BYTES = int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(16 * 1024 * 1024)))
DEBUG_LOG = os.environ.get("ABAQUS_MCP_DEBUG", "").lower() in ("1", "true", "yes", "on")
LOG_PATH = os.environ.get("ABAQUS_MCP_LOG") or os.path.join(tempfile.gettempdir(), "abaqus_mcp_plugin.log")
LOG_LIMIT = 1024 * 1024
POLL_MILLISECONDS = 50
BATCH_SIZE = 5
MENU_MODULES = ["Part", "Property", "Assembly", "Step", "Interaction", "Load", "Mesh", "Job", "Visualization"]

_LOG_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_REQUESTS = queue.Queue()
_STOP_REQUESTED = threading.Event()
_STATS = {"processed": 0, "started_at": None}
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
        chunk = sock.recv(4096)
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


# 下面的模板在 Abaqus 内核中执行：在持久命名空间里运行用户代码，捕获输出与错误诊断，
# 再把结果原子地写入 JSON 文件，由图形界面线程读取
_KERNEL_TEMPLATE = r'''
import ast
import base64
import builtins
import contextlib
import difflib
import inspect
import json
import linecache
import os
import re
import sys
import traceback
from abaqus import mdb as _abaqus_mcp_mdb, session as _abaqus_mcp_session

code = base64.b64decode("__ABAQUS_MCP_CODE__").decode("utf-8")
response_path = base64.b64decode("__ABAQUS_MCP_RESPONSE__").decode("utf-8")
execution_id = base64.b64decode("__ABAQUS_MCP_ID__").decode("ascii")

def _jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except Exception:
        try:
            description = repr(value)
        except Exception as exc:
            description = "<repr failed: %s>" % type(exc).__name__
        return {
            "repr": description[:4000],
            "type": "%s.%s" % (type(value).__module__, type(value).__name__),
        }

def _node_source(node):
    try:
        return ast.unparse(node)
    except Exception:
        return None

def _key_literal(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Index):
        return _key_literal(node.value)
    return None

def _extract_tb_lineno(exc):
    if hasattr(exc, "lineno") and getattr(exc, "lineno") is not None:
        return getattr(exc, "lineno")
    tb = exc.__traceback__
    lineno = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename.startswith("<abaqus-mcp:"):
            lineno = tb.tb_lineno
        tb = tb.tb_next
    return lineno

def _find_subscript_parent(source, missing_key, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _key_literal(node.slice) == missing_key:
            src = _node_source(node.value)
            if src is not None:
                candidates.append((getattr(node, "lineno", 0), src))
    if not candidates and lineno is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                node_lineno = getattr(node, "lineno", None)
                if node_lineno == lineno:
                    src = _node_source(node.value)
                    if src is not None:
                        candidates.append((node_lineno, src))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda c: abs(c[0] - lineno))
    return candidates[0][1]

def _find_attribute_parent(source, missing_attr, lineno=None):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == missing_attr:
            candidates.append((getattr(node, "lineno", 0), _node_source(node.value)))
    if not candidates:
        return None
    if lineno is not None:
        candidates.sort(key=lambda item: abs(item[0] - lineno))
    return candidates[0][1]

def _extract_code_excerpt(code, lineno, radius=2):
    if lineno is None:
        return None
    lines = code.splitlines()
    if not lines:
        return None
    index = max(0, lineno - 1)
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    excerpt_lines = []
    for i in range(start, end):
        prefix = ">>" if i == index else "  "
        excerpt_lines.append("%s %4d | %s" % (prefix, i + 1, lines[i]))
    return "\n".join(excerpt_lines)

def _resolve_simple_expr(expr, namespace):
    def _eval_node(node):
        if isinstance(node, ast.Name):
            if node.id in namespace:
                return namespace[node.id]
            return getattr(builtins, node.id)
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = _eval_node(node.value)
            try:
                key = ast.literal_eval(node.slice)
            except Exception:
                if isinstance(node.slice, ast.Name):
                    key = namespace[node.slice.id]
                elif isinstance(node.slice, ast.Index):
                    if isinstance(node.slice.value, ast.Constant):
                        key = node.slice.value.value
                    elif isinstance(node.slice.value, ast.Name):
                        key = namespace[node.slice.value.id]
                    else:
                        raise
                else:
                    raise
            return base[key]
        raise ValueError("unsupported expression node")

    parsed = ast.parse(expr, mode="eval")
    return _eval_node(parsed.body)

def _extract_params_from_sig(sig_str):
    m = re.search(r"\((.*)\)", sig_str)
    if not m:
        return []
    content = m.group(1)
    params = []
    paren_depth = 0
    current_param = []
    for char in content:
        if char in "([{":
            paren_depth += 1
            current_param.append(char)
        elif char in ")]}":
            paren_depth -= 1
            current_param.append(char)
        elif char == "," and paren_depth == 0:
            params.append("".join(current_param).strip())
            current_param = []
        else:
            current_param.append(char)
    if current_param:
        params.append("".join(current_param).strip())
    names = []
    for p in params:
        if not p:
            continue
        word = re.match(r"^([a-zA-Z_]\w*)", p)
        if word:
            name = word.group(1)
            if name not in ("self", "args", "kwargs"):
                names.append(name)
    return names

def _extract_invalid_keyword(msg):
    patterns = (
        r"got an unexpected keyword argument ['\"](\w+)['\"]",
        r"['\"](\w+)['\"] is an invalid keyword argument",
        r"keyword error on (\w+)",
    )
    for pattern in patterns:
        match = re.search(pattern, msg, re.I)
        if match:
            return match.group(1)
    return None

def _extract_call_target(code, lineno):
    if lineno is None:
        return None
    try:
        tree = ast.parse(code)
    except Exception:
        try:
            lines = code.splitlines()
            if lineno - 1 < 0 or lineno - 1 >= len(lines):
                return None
            tree = ast.parse(lines[lineno - 1], mode="exec")
        except Exception:
            return None
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start is not None and end is not None:
                if start <= lineno <= end:
                    candidates.append(node)
            elif start == lineno:
                candidates.append(node)
    if not candidates:
        return None
    candidates.sort(key=lambda n: getattr(n, "end_lineno", getattr(n, "lineno", 0)) - getattr(n, "lineno", 0))
    return _node_source(candidates[0].func)

def _summarize_mapping_keys(mapping_obj, missing_key):
    result = {"possible_keys": []}
    keys_method = getattr(mapping_obj, "keys", None)
    if not callable(keys_method):
        return result
    try:
        key_texts = [str(k) for k in list(keys_method())]
    except Exception:
        return result
    result["available_key_count"] = len(key_texts)
    if missing_key is not None:
        try:
            result["possible_keys"] = difflib.get_close_matches(str(missing_key), key_texts, n=3, cutoff=0.6)
        except Exception:
            pass
    if not result["possible_keys"]:
        result["available_keys_sample"] = key_texts[:5]
    return result

def _summarize_object_members(obj, missing_attr):
    result = {"possible_members": []}
    try:
        members = sorted([name for name in dir(obj) if not name.startswith("_")])
    except Exception:
        return result
    if missing_attr:
        try:
            result["possible_members"] = difflib.get_close_matches(missing_attr, members, n=3, cutoff=0.6)
        except Exception:
            pass
    return result

def _extract_signature_from_docstring(doc, target_name):
    pattern = r"\b" + re.escape(target_name) + r"\s*\("
    match = re.search(pattern, doc)
    if not match:
        return None
    start_pos = match.start()
    open_paren_idx = match.end() - 1
    paren_count = 0
    end_pos = -1
    for i in range(open_paren_idx, min(open_paren_idx + 1000, len(doc))):
        char = doc[i]
        if char == "(":
            paren_count += 1
        elif char == ")":
            paren_count -= 1
            if paren_count == 0:
                end_pos = i + 1
                break
    if end_pos != -1:
        return " ".join(doc[start_pos:end_pos].split())
    return None

def _summarize_callable(target_expr, namespace, invalid_keyword=None):
    summary = {"call_target": target_expr, "callable_signature": None, "callable_summary": None}
    if not target_expr:
        return summary
    try:
        target = _resolve_simple_expr(target_expr, namespace)
    except Exception:
        return summary
    target_name = getattr(target, "__name__", "function")
    try:
        sig_str = "%s%s" % (target_name, inspect.signature(target))
    except Exception:
        sig_str = "%s(...)" % target_name
    doc = None
    try:
        doc = inspect.getdoc(target)
    except Exception:
        pass
    if not doc:
        try:
            doc = getattr(target, "__doc__", None)
        except Exception:
            pass
    if doc:
        try:
            lines = doc.strip().splitlines()
            if lines:
                first_line = lines[0].strip()
                if " -> " in first_line:
                    first_line = first_line.split(" -> ", 1)[1].strip()
                summary["callable_summary"] = first_line if first_line else None
            if sig_str.endswith("(...)"):
                extracted_sig = _extract_signature_from_docstring(doc, target_name)
                if extracted_sig:
                    sig_str = extracted_sig
        except Exception:
            pass
    if len(sig_str) > 320:
        sig_str = sig_str[:317] + "..."
    summary["callable_signature"] = sig_str
    if invalid_keyword:
        try:
            valid_params = []
            try:
                sig = inspect.signature(target)
                valid_params = [p.name for p in sig.parameters.values() if p.name not in ("self", "args", "kwargs")]
            except Exception:
                pass
            if not valid_params:
                valid_params = _extract_params_from_sig(sig_str)
            if valid_params:
                matches = difflib.get_close_matches(invalid_keyword, valid_params, n=3, cutoff=0.6)
                if matches:
                    summary["possible_keywords"] = matches
        except Exception:
            pass
    return summary

def _is_user_source(filename):
    if filename.startswith("<abaqus-mcp:"):
        return True
    if not filename or filename.startswith("<") or not os.path.isfile(filename):
        return False
    normalized = os.path.normcase(os.path.abspath(filename))
    portable = normalized.replace("\\", "/").lower()
    if "/site-packages/" in portable or "/dist-packages/" in portable:
        return False
    for root in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.dirname(traceback.__file__))):
        try:
            root_path = os.path.normcase(os.path.abspath(root))
            if os.path.commonpath((normalized, root_path)) == root_path:
                return False
        except (OSError, ValueError):
            pass
    return True

def _format_execution_error(source, exc, namespace=None):
    tb_str = traceback.format_exc()
    tb_lines = [l for l in tb_str.strip().splitlines() if l.strip()]
    core_error = tb_lines[-1] if tb_lines else str(exc)
    exc_type_name = type(exc).__name__
    error_module = type(exc).__module__
    if not isinstance(error_module, str):
        error_module = getattr(error_module, "__name__", str(error_module))
    error_type = "%s.%s" % (error_module, exc_type_name)
    tb = exc.__traceback__
    user_frames = []
    while tb is not None:
        if _is_user_source(tb.tb_frame.f_code.co_filename):
            user_frames.append(tb)
        tb = tb.tb_next
    primary = user_frames[-1] if user_frames else None
    syntax_file = getattr(exc, "filename", None) if isinstance(exc, SyntaxError) else None
    if syntax_file and _is_user_source(syntax_file):
        lineno = getattr(exc, "lineno", None)
        primary_frame = {"file": syntax_file, "line": lineno, "function": "<module>"}
    elif primary:
        lineno = primary.tb_lineno
        primary_frame = {"file": primary.tb_frame.f_code.co_filename,
                         "line": lineno, "function": primary.tb_frame.f_code.co_name}
    else:
        lineno = _extract_tb_lineno(exc)
        primary_frame = None
    source_file = primary_frame.get("file") if primary_frame else None
    if source_file and not source_file.startswith("<"):
        linecache.checkcache(source_file)
    source_for_error = "".join(linecache.getlines(source_file)) if source_file else source
    if not source_for_error:
        source_for_error = source
    diagnostic_namespace = dict(namespace or {})
    if primary is not None:
        diagnostic_namespace.update(primary.tb_frame.f_globals)
        diagnostic_namespace.update(primary.tb_frame.f_locals)
    code_excerpt = _extract_code_excerpt(source_for_error, lineno)
    source_lines = source_for_error.splitlines()
    code_line = (source_lines[lineno - 1].strip()[:240]
                 if lineno is not None and 0 < lineno <= len(source_lines) else None)

    is_key_error = isinstance(exc, KeyError) or exc_type_name.endswith("KeyError")
    is_attribute_error = isinstance(exc, AttributeError) or exc_type_name.endswith("AttributeError")
    is_name_error = exc_type_name == "NameError"
    is_type_error = isinstance(exc, TypeError) or exc_type_name.endswith("TypeError")
    is_syntax_error = isinstance(exc, SyntaxError) or exc_type_name.endswith("SyntaxError")

    detected_missing_key = None
    detected_parent_path = None
    if lineno is not None:
        try:
            lines = source_for_error.splitlines()
            if 0 <= lineno - 1 < len(lines):
                tree = ast.parse(lines[lineno - 1])
                for node in ast.walk(tree):
                    if isinstance(node, ast.Subscript):
                        val_key = None
                        if isinstance(node.slice, ast.Constant):
                            val_key = node.slice.value
                        elif isinstance(node.slice, ast.Name):
                            val_key = diagnostic_namespace.get(node.slice.id)
                        elif isinstance(node.slice, ast.Index):
                            if isinstance(node.slice.value, ast.Constant):
                                val_key = node.slice.value.value
                            elif isinstance(node.slice.value, ast.Name):
                                val_key = diagnostic_namespace.get(node.slice.value.id)
                        if val_key is not None:
                            p_path = _node_source(node.value)
                            if p_path:
                                try:
                                    p_obj = _resolve_simple_expr(p_path, diagnostic_namespace)
                                    keys_method = getattr(p_obj, "keys", None)
                                    if callable(keys_method):
                                        try:
                                            keys_list = list(keys_method())
                                            is_missing = val_key not in keys_list
                                        except Exception:
                                            is_missing = val_key not in p_obj
                                        if is_missing:
                                            is_key_error = True
                                            detected_missing_key = val_key
                                            detected_parent_path = p_path
                                            break
                                except Exception:
                                    pass
        except Exception:
            pass

    if is_key_error:
        missing_key = detected_missing_key if detected_missing_key is not None else (exc.args[0] if exc.args else None)
        parent_path = detected_parent_path if detected_parent_path is not None else _find_subscript_parent(source_for_error, missing_key, lineno)
        recovery = {"missing_key": _jsonable(missing_key), "parent_object_path": parent_path}
        if parent_path:
            try:
                recovery.update(_summarize_mapping_keys(_resolve_simple_expr(parent_path, diagnostic_namespace), missing_key))
            except Exception:
                pass
    elif is_attribute_error:
        missing_attr = getattr(exc, "name", None)
        source_obj = getattr(exc, "obj", None)
        if not missing_attr:
            match = re.search(r"has no attribute ['\"](\w+)['\"]", str(exc))
            if match:
                missing_attr = match.group(1)
        parent_path = _find_attribute_parent(source_for_error, missing_attr, lineno) if missing_attr else None
        if source_obj is None and parent_path:
            try:
                source_obj = _resolve_simple_expr(parent_path, diagnostic_namespace)
            except Exception:
                pass
        recovery = {
            "missing_attribute": missing_attr,
            "object_type": type(source_obj).__name__ if source_obj is not None else None,
            "parent_object_path": parent_path,
        }
        if source_obj is not None:
            try:
                recovery.update(_summarize_object_members(source_obj, missing_attr))
            except Exception:
                pass
    elif is_name_error:
        missing_var = getattr(exc, "name", None)
        if not missing_var:
            m = re.search(r"name '(\w+)' is not defined", str(exc))
            if m:
                missing_var = m.group(1)
        recovery = {"missing_variable": missing_var}
        if missing_var:
            names = set(str(key) for key in diagnostic_namespace if isinstance(key, str))
            names.update(dir(builtins))
            matches = difflib.get_close_matches(missing_var, sorted(names), n=3, cutoff=0.6)
            if matches:
                recovery["possible_names"] = matches
        abaqus_modules = {
            "mesh", "part", "material", "assembly", "step", "interaction",
            "load", "section", "sketch", "job", "connector", "visualization",
            "xyPlot", "displayGroup", "meshEdit", "connectorBehavior", "symbolicConstants",
        }
        if missing_var in abaqus_modules:
            recovery["import_suggestion"] = "from abaqus import %s" % missing_var
        elif missing_var == "C":
            recovery["import_suggestion"] = "from abaqusConstants import *"
        elif missing_var and missing_var.isupper() and len(missing_var) > 1:
            recovery["import_suggestion"] = "from abaqusConstants import *"
    elif is_syntax_error:
        recovery = {
            "syntax_line": getattr(exc, "lineno", None),
            "syntax_offset": getattr(exc, "offset", None),
            "syntax_text": getattr(exc, "text", None),
        }
    elif is_type_error:
        call_target = _extract_call_target(source_for_error, lineno)
        recovery = {"call_target": call_target}
        recovery.update(_summarize_callable(call_target, diagnostic_namespace, _extract_invalid_keyword(core_error)))
    else:
        recovery = {}

    return {
        "ok": False,
        "core_error": core_error,
        "message": getattr(exc, "msg", str(exc)) if isinstance(exc, SyntaxError) else str(exc),
        "error_type": error_type,
        "error_line": lineno,
        "primary_frame": primary_frame,
        "traceback": tb_str,
        "frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ],
        "code_excerpt": code_excerpt,
        "code_line": code_line,
        "recovery": recovery,
    }

_MAX_OUTPUT = 4000

class _LimitedWriter:
    encoding = "utf-8"

    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.count = 0

    def write(self, value):
        length = len(value)
        if self.count < self.limit:
            self.parts.append(value[:self.limit - self.count])
        self.count += length
        return length

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        result = "".join(self.parts)
        if self.count > self.limit:
            result += "\n... (truncated, total %d chars)" % self.count
        return result

namespace = globals().setdefault("_ABAQUS_MCP_GLOBALS", {"__name__": "__abaqus_mcp_exec__", "__doc__": None})
namespace.update({"mdb": _abaqus_mcp_mdb, "session": _abaqus_mcp_session})
namespace.pop("result", None)

filename = "<abaqus-mcp:%s>" % execution_id
if len(code) <= 262144:
    keys = globals().setdefault("_ABAQUS_MCP_SOURCE_KEYS", [])
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    keys.append(filename)
    if len(keys) > 128:
        linecache.cache.pop(keys.pop(0), None)

stdout = _LimitedWriter(_MAX_OUTPUT)
stderr = _LimitedWriter(_MAX_OUTPUT)
returned = None
error_response = None

try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            parsed = ast.parse(code, filename=filename, mode="exec")
            if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr):
                compiled = compile(ast.Expression(body=parsed.body[0].value), filename, "eval")
                returned = eval(compiled, namespace, namespace)
            else:
                exec(compile(parsed, filename, "exec"), namespace, namespace)
                returned = namespace.pop("result", None)
        except Exception as exc:
            error_response = _format_execution_error(code, exc, namespace)
except Exception as exc:
    error_response = _format_execution_error(code, exc)
finally:
    namespace.pop("result", None)

if error_response is not None:
    error_response["executionId"] = execution_id
    error_response["stdout"] = stdout.getvalue()
    error_response["stderr"] = stderr.getvalue()
    payload = {"ok": True, "result": error_response}
else:
    payload = {
        "ok": True,
        "result": {
            "ok": True,
            "executionId": execution_id,
            "return_value": _jsonable(returned),
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        },
    }

temporary_path = response_path + ".tmp"
with open(temporary_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
os.replace(temporary_path, response_path)
'''


def _kernel_command(code, response_path, execution_id):
    return (
        _KERNEL_TEMPLATE.replace("__ABAQUS_MCP_CODE__", base64.b64encode(code.encode("utf-8")).decode("ascii"))
        .replace("__ABAQUS_MCP_RESPONSE__", base64.b64encode(response_path.encode("utf-8")).decode("ascii"))
        .replace("__ABAQUS_MCP_ID__", base64.b64encode(execution_id.encode("ascii")).decode("ascii"))
    )


def _run_kernel_code(code, timeout, execution_id):
    response_path = os.path.join(tempfile.gettempdir(), "abaqus_mcp_%s.json" % uuid.uuid4().hex)
    _log("sendCommand start response_path=%s" % response_path)
    sendCommand(_kernel_command(code, response_path, execution_id), False, False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(response_path):
            with open(response_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            os.remove(response_path)
            result = payload["result"]
            if result.get("executionId") != execution_id:
                raise RuntimeError("kernel response executionId mismatch")
            _log("kernel response ok=%s" % result.get("ok"))
            return result
        time.sleep(0.03)
    raise TimeoutError("timed out waiting for Abaqus kernel response")


_PING_CODE = (
    "import os, sys, platform\n"
    "import abaqus\n"
    "from abaqus import mdb, session\n"
    "result = {'python': sys.version, 'executable': sys.executable, "
    "'platform': platform.platform(), 'pid': os.getpid(), "
    "'cpu_count': os.cpu_count(), 'cwd': os.getcwd(), "
    "'abaqus_version': abaqus.version, "
    "'models': list(mdb.models.keys()), "
    "'viewports': list(session.viewports.keys())}"
)


def _bridge_status():
    started_at = _STATS["started_at"]
    return {
        "version": __version__,
        "host": HOST,
        "port": PORT,
        "transport": "socket",
        "running": _SERVER is not None,
        "processed": _STATS["processed"],
        "queued": _REQUESTS.qsize(),
        "uptime_seconds": int(time.time() - started_at) if started_at is not None and _SERVER is not None else 0,
        "debug_log": LOG_PATH if DEBUG_LOG else None,
    }


def _handle_on_gui_thread(item):
    params = item.params
    timeout = float(params.get("timeout") or DEFAULT_TIMEOUT)
    execution_id = params.get("executionId", "")
    if item.method == "ping":
        execution = _run_kernel_code(_PING_CODE, timeout, execution_id)
        if not execution["ok"]:
            raise RuntimeError("ping failed in the Abaqus kernel: %s" % execution["core_error"])
        result = execution["return_value"]
        bridge = _bridge_status()
        bridge.update({
            "gui_python": sys.version,
            "gui_platform": platform.platform(),
            "gui_thread": threading.current_thread().name,
        })
        result["bridge"] = bridge
        return result
    if item.method == "execute":
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("params.code must be a non-empty string")
        return _run_kernel_code(code, timeout, execution_id)
    raise ValueError("unknown method: %r" % item.method)


class GuiRequest:
    def __init__(self, method, params, deadline):
        self.method = method
        self.params = params
        self.deadline = deadline
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.state = "queued"
        self.result = None
        self.error = None

    def claim(self):
        # 图形界面主线程取出请求时判定：仍在期限内则开始执行，已超过期限则取消且不再执行。
        # CAE 执行内核命令期间可能暂停插件的后台线程，取消不能只依赖处理请求的线程；
        # 期限由服务端按发出时刻计算，插件晚读到请求也不会影响判定
        with self.lock:
            if self.state != "queued":
                return False
            if time.time() >= self.deadline:
                self.state = "cancelled"
                self.event.set()
                return False
            self.state = "running"
            return True

    def settle(self):
        # 处理请求的线程等待结束后调用：仍在排队则取消，返回请求的最终状态
        with self.lock:
            if self.state == "queued":
                self.state = "cancelled"
            return self.state


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
            if _DISPATCHER is None:
                raise RuntimeError("GUI dispatcher is not initialized")
            item = GuiRequest(method, params, float(params["deadline"]))
            _REQUESTS.put(item)
            item.event.wait(max(0.0, item.deadline - time.time()))
            state = item.settle()
            if state == "cancelled":
                raise TimeoutError("timed out before GUI execution; request cancelled")
            if state == "running":
                raise TimeoutError("timed out while GUI execution was running; outcome unknown")
            if item.error is not None:
                raise item.error
            _send(self.request, {"id": request_id, "ok": True, "result": item.result})
            _log("response ok id=%s" % request_id)
        except Exception as exc:
            _log("response error id=%s type=%s" % (request_id, type(exc).__name__))
            _send(self.request, {
                "id": request_id,
                "ok": False,
                "error": {
                    "message": str(exc),
                    "type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                    "traceback": traceback.format_exc(),
                    "state": "CANCELLED" if "request cancelled" in str(exc) else "UNKNOWN",
                },
            })


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
            self._start()
        elif self.action == "stop":
            self._stop()
        else:
            self._status()
        return 1

    def _start(self):
        try:
            started = start_bridge()
        except Exception as exc:
            _log("bridge start failed type=%s" % type(exc).__name__)
            showAFXErrorDialog(getAFXApp().getAFXMainWindow(), "Abaqus MCP bridge failed to start: %s" % exc)
            return
        if not started:
            _announce("Abaqus MCP bridge is already running on %s:%s." % (HOST, PORT))
            return
        self._schedule_poll()
        _announce("Abaqus MCP %s bridge listening on %s:%s." % (__version__, HOST, PORT))
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
                item = _REQUESTS.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if not item.claim():
                continue
            try:
                _log("GUI thread handling method=%s" % item.method)
                item.result = _handle_on_gui_thread(item)
            except Exception as exc:
                item.error = exc
                _log("GUI thread error method=%s type=%s" % (item.method, type(exc).__name__))
            finally:
                with item.lock:
                    item.state = "done"
                _STATS["processed"] += 1
                item.event.set()
        if _SERVER is not None:
            self._schedule_poll()
        return 1


_toolset = getAFXApp().getAFXMainWindow().getPluginToolset()
_toolset.registerGuiMenuButton(
    object=McpGuiActionForm(_toolset, "start"),
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
_announce("Abaqus MCP %s plugin loaded. Use Plug-ins > Abaqus MCP > Start MCP Bridge." % __version__)

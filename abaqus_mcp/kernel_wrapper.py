# 每次调用时由服务端发给插件、在 Abaqus 内核中执行的包装代码。插件只负责传递：它把请求编码后填入
# 内核命令，包装代码在一个独立字典中运行，取得 _request（请求）与 _kernel_globals（内核 __main__
# 的全局变量，用于保存各对话的命名空间），执行用户代码后把结果原子地写入 _request['response_path']

PROTOCOL = 2

WRAPPER = r'''
import ast
import builtins
import collections
import contextlib
import difflib
import importlib
import importlib.util
import inspect
import json
import linecache
import math
import os
import re
import sys
import time
import traceback
import types

request = _request
execution_id = request["execution_id"]
response_path = request["response_path"]

MAX_NAMESPACES = 32
MAX_OUTPUT = 4000
MAX_REPLACED = 50
PRELOAD = (
    "from abaqus import *\n"
    "from abaqusConstants import *\n"
    "from caeModules import *\n"
    "import regionToolset\n"
    "import odbAccess\n"
    # from abaqus import * 用 Abaqus 的 XY 数据函数替换了内置的 sum 与 pow；恢复内置函数，
    # Abaqus 的版本仍可通过 abaqus.sum、abaqus.pow 调用
    "from builtins import sum, pow\n"
)
ABAQUS_MODULE_NAMES = (
    "assembly", "connectorBehavior", "interaction", "job", "load", "material", "mesh", "optimization",
    "part", "regionToolset", "section", "sketch", "step", "visualization", "xyPlot", "odbAccess",
)
HELPERS = r"""
def abaqus_error_text(error):
    # 部分 Abaqus 异常把 UTF-8 字节按 GBK 解码，得到乱码；把乱码还原，正常文本原样返回
    text = str(error)
    try:
        return text.encode('gbk').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
"""


def _repair_text(text):
    if not isinstance(text, str):
        return text
    lines = []
    for line in text.split("\n"):
        try:
            lines.append(line.encode("gbk").decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            lines.append(line)
    return "\n".join(lines)


def _to_json(value, path, replaced, active):
    if value is None or isinstance(value, str):
        return value
    module = type(value).__module__
    if module == "symbolicConstants":
        # AbaqusBoolean 继承 int，ON/OFF 转为布尔值；其余符号常量转为名称
        if isinstance(value, int) and type(value).__name__ == "AbaqusBoolean":
            return bool(value)
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isfinite(value):
            # Abaqus 把浮点数按 15 位有效数字输出，接近 DBL_MAX 的值舍入后超出范围，读取时变成无穷大；改用完整精度的文本
            if math.isinf(float(repr(value))):
                _note(replaced, path, value)
                return "%.17g" % value
            return float(value)
        _note(replaced, path, value)
        return "NaN" if value != value else ("Infinity" if value > 0 else "-Infinity")
    if module == "numpy":
        if hasattr(value, "tolist"):
            return _to_json(value.tolist(), path, replaced, active)
    if id(value) in active:
        _note(replaced, path, value)
        return {"repr": "<circular reference>", "type": "%s.%s" % (module, type(value).__name__)}
    if isinstance(value, dict):
        active.add(id(value))
        converted = {}
        for key, item in value.items():
            text_key = key if isinstance(key, str) else str(key)
            converted[text_key] = _to_json(item, "%s.%s" % (path, text_key), replaced, active)
        active.discard(id(value))
        return converted
    if isinstance(value, (list, tuple, set, frozenset)):
        active.add(id(value))
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        converted = [_to_json(item, "%s[%d]" % (path, index), replaced, active) for index, item in enumerate(items)]
        active.discard(id(value))
        return converted
    _note(replaced, path, value)
    try:
        description = repr(value)
    except Exception as exc:
        description = "<repr failed: %s>" % type(exc).__name__
    return {"repr": description[:4000], "type": "%s.%s" % (module, type(value).__name__)}


def _note(replaced, path, value):
    if len(replaced) < MAX_REPLACED:
        replaced.append({"path": path, "type": "%s.%s" % (type(value).__module__, type(value).__name__)})


def _jsonable(value):
    replaced = []
    return _to_json(value, "value", replaced, set()), replaced


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
    # 只求值名称、常量、属性与下标，不执行函数调用，避免诊断时产生副作用
    def _eval_node(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Tuple):
            return tuple(_eval_node(item) for item in node.elts)
        if isinstance(node, ast.Name):
            if node.id in namespace:
                return namespace[node.id]
            return getattr(builtins, node.id)
        if isinstance(node, ast.Attribute):
            return getattr(_eval_node(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            return _eval_node(node.value)[_eval_node(node.slice)]
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
        word = re.match(r"^<?\s*([a-zA-Z_]\w*)", p)
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
        r"关键字错误\s*[:：]\s*(\w+)",
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
    if invalid_keyword:
        summary["invalid_keyword"] = invalid_keyword
    if not target_expr:
        return summary
    try:
        target = _resolve_simple_expr(target_expr, namespace)
    except Exception:
        return summary
    target_name = getattr(target, "__name__", "function")
    try:
        sig_str = "%s%s" % (target_name, inspect.signature(target))
        summary["signature_source"] = "inspect.signature"
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
                    # Abaqus 2024 的 __doc__ 对部分方法给出过时的关键字，标注来源
                    summary["signature_source"] = "__doc__ (may be outdated; check with api_help)"
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
    raw_message = getattr(exc, "msg", str(exc)) if isinstance(exc, SyntaxError) else str(exc)
    message = _repair_text(raw_message)

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
        recovery = {"missing_key": _jsonable(missing_key)[0], "parent_object_path": parent_path}
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
            recovery["import_suggestion"] = "import %s" % missing_var
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
        recovery.update(_summarize_callable(call_target, diagnostic_namespace,
                                            _extract_invalid_keyword(message)))
    else:
        recovery = {}

    error = {
        "ok": False,
        "core_error": _repair_text(core_error),
        "message": message,
        "error_type": error_type,
        "error_line": lineno,
        "primary_frame": primary_frame,
        "traceback": _repair_text(tb_str),
        "frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ],
        "code_excerpt": code_excerpt,
        "code_line": code_line,
        "recovery": recovery,
    }
    if message != raw_message:
        error["raw_message"] = raw_message
    return error


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


def _new_namespace(name):
    # 对话命名空间按 CAE 命令行预先导入；工具自带的代码（name 为 None）自行导入所需模块
    namespace = {"__name__": "__abaqus_mcp_exec__", "__doc__": None}
    exec((PRELOAD if name is not None else "") + HELPERS, namespace)
    return {"name": name, "globals": namespace, "createdAt": time.time(), "lastUsedAt": None}


def _namespace_info(entry, created):
    return {"name": entry["name"], "exists": True, "created": created, "names": len(entry["globals"]),
            "createdAt": entry["createdAt"], "previousUseAt": entry["lastUsedAt"],
            "namespaces": len(_store)}


_store = _kernel_globals.setdefault("_ABAQUS_MCP_NAMESPACES", collections.OrderedDict())


def _select_namespace(name, reset=False):
    if name is None:
        return _new_namespace(None), True
    created = reset or name not in _store
    if created:
        _store[name] = _new_namespace(name)
    _store.move_to_end(name)
    while len(_store) > MAX_NAMESPACES:
        _store.popitem(last=False)
    return _store[name], created


def _register_source(filename, code):
    if len(code) <= 262144:
        keys = _kernel_globals.setdefault("_ABAQUS_MCP_SOURCE_KEYS", [])
        linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
        keys.append(filename)
        if len(keys) > 128:
            linecache.cache.pop(keys.pop(0), None)


def _module_bindings(namespace):
    return {name: namespace.get(name) for name in ABAQUS_MODULE_NAMES}


def _shadow_warnings(before, namespace):
    warnings = []
    for name, previous in before.items():
        current = namespace.get(name)
        # isinstance 会读取 __class__，已删除的 Abaqus 对象在读取任何属性时抛出 AccessError；type() 不读取属性
        if current is not previous and current is not None and not issubclass(type(current), types.ModuleType):
            warnings.append("The name %r now refers to an object of type %s and no longer to the Abaqus module %s; "
                            "import the module under another name (import %s as %smod) when you need it."
                            % (name, type(current).__name__, name, name, name))
    return warnings


def _under(path, directories):
    normalized = os.path.normcase(os.path.abspath(path))
    for directory in directories:
        root = os.path.normcase(os.path.abspath(directory))
        try:
            if os.path.commonpath((normalized, root)) == root:
                return True
        except ValueError:
            pass
    return False


def _run_code(code, namespace):
    filename = "<abaqus-mcp:%s>" % execution_id
    _register_source(filename, code)
    namespace.pop("result", None)
    try:
        parsed = ast.parse(code, filename=filename, mode="exec")
        if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr):
            return eval(compile(ast.Expression(body=parsed.body[0].value), filename, "eval"), namespace, namespace)
        exec(compile(parsed, filename, "exec"), namespace, namespace)
        return namespace.pop("result", None)
    finally:
        namespace.pop("result", None)


def _run_script(script, namespace):
    path = os.path.abspath(script["path"])
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    removed = []
    for module_name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if module_file and script["fresh_modules"] and _under(module_file, script["fresh_modules"]):
            del sys.modules[module_name]
            removed.append(module_name)
            # .pyc 按源文件的整秒修改时间与大小判定是否过期，同一秒内改写且大小不变时会沿用旧代码，删除缓存后从源文件重新编译
            if module_file.endswith(".py"):
                cached = importlib.util.cache_from_source(module_file)
                if os.path.exists(cached):
                    os.remove(cached)
    if removed:
        importlib.invalidate_caches()
    script_globals = {"__name__": "__main__", "__file__": path, "__doc__": None, "__builtins__": builtins}
    script_globals.update(script["variables"])
    namespace["last_script_globals"] = script_globals
    script_dir = os.path.dirname(path)
    previous_argv = sys.argv
    previous_cwd = os.getcwd()
    sys.argv = [path] + list(script["args"])
    sys.path.insert(0, script_dir)
    try:
        if script["cwd"]:
            os.chdir(script["cwd"])
        started = time.time()
        exec(compile(source, path, "exec"), script_globals)
        elapsed = time.time() - started
    finally:
        os.chdir(previous_cwd)
        sys.argv = previous_argv
        if sys.path and sys.path[0] == script_dir:
            sys.path.pop(0)
        else:
            sys.path.remove(script_dir)
    name = script["result_variable"]
    if name not in script_globals:
        raise KeyError("The script finished without defining %r; set it in the script or choose result_variable" % name)
    return {"path": path, "seconds": round(elapsed, 3), "freshModules": sorted(removed), "value": script_globals[name]}


stdout = _LimitedWriter(MAX_OUTPUT)
stderr = _LimitedWriter(MAX_OUTPUT)
returned = None
error_response = None
warnings = []
entry = None
created = False
source_for_errors = request.get("code") or ""

try:
    entry, created = _select_namespace(request.get("namespace"), request["mode"] == "reset")
    namespace = entry["globals"]
    if entry["name"] is None:
        # 工具自带的代码在独立命名空间中运行，可以读取 CAE 的启动目录
        namespace["__abaqus_mcp_gui_cwd__"] = request.get("gui_cwd")
    before = _module_bindings(namespace)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            if request["mode"] == "code":
                returned = _run_code(request["code"], namespace)
            elif request["mode"] == "script":
                returned = _run_script(request["script"], namespace)
            elif request["mode"] == "reset":
                returned = {"reset": True}
            else:
                raise ValueError("unknown mode %r" % request["mode"])
        except Exception as exc:
            error_response = _format_execution_error(source_for_errors, exc, namespace)
    if entry["name"] is not None:
        warnings = _shadow_warnings(before, namespace)
except Exception as exc:
    error_response = _format_execution_error(source_for_errors, exc)

response = {"executionId": execution_id, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
if entry is not None and entry["name"] is not None:
    response["namespace"] = _namespace_info(entry, created)
    entry["lastUsedAt"] = time.time()
described = request.get("describe_namespace")
if described is not None:
    # ping 只查看调用方的命名空间，不创建
    response["namespace"] = (_namespace_info(_store[described], False) if described in _store
                             else {"name": described, "exists": False, "namespaces": len(_store)})
if warnings:
    response["warnings"] = warnings
if error_response is not None:
    response.update(error_response)
else:
    value, replaced = _jsonable(returned)
    response.update({"ok": True, "return_value": value})
    if replaced:
        response["replaced"] = replaced

temporary_path = response_path + ".tmp"
with open(temporary_path, "w", encoding="utf-8") as handle:
    json.dump({"ok": True, "result": response}, handle, ensure_ascii=False, allow_nan=False)
os.replace(temporary_path, response_path)
'''

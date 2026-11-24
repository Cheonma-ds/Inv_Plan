import ast
import copy
import datetime as _dt
import decimal
import io
import json
import math
import statistics
import sys
import time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

ALLOWED_MODULES = frozenset({"math", "statistics", "datetime", "decimal", "json"})

FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "breakpoint",
        "input",
        "exit",
        "quit",
        "type",
        "memoryview",
        "bytearray",
        "object",
        "super",
        "classmethod",
        "staticmethod",
        "property",
    }
)

FORBIDDEN_ATTRS = frozenset(
    {
        "now",
        "utcnow",
        "today",
        "system",
        "popen",
        "spawn",
        "fork",
        "unlink",
        "remove",
        "rmdir",
        "mkdir",
        "write_text",
        "write_bytes",
        "read_text",
        "read_bytes",
    }
)

ALLOWED_AST_NODES: tuple[type[ast.AST], ...] = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.AnnAssign,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Pass,
    ast.Return,
    ast.FunctionDef,
    ast.Lambda,
    ast.IfExp,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Constant,
    ast.Name,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.arguments,
    ast.arg,
    ast.keyword,
    ast.Load,
    ast.Store,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.And,
    ast.Or,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Invert,
    ast.Try,
    ast.ExceptHandler,
)

MAX_SCRIPT_CHARS = 32_000
MAX_RANGE_ITEMS = 1_000_000
MAX_SEQ_MULT_LEN = 1_000_000
MAX_POW_EXPONENT = 10_000


class SandboxError(RuntimeError):
    """Base error for sandbox validation, timeout, and execution failures."""


class SandboxSecurityError(SandboxError):
    """Raised when a calculation script attempts disallowed imports, I/O, or attributes."""


class SandboxTimeoutError(SandboxError):
    """Raised when a calculation script exceeds execution timeout or step limits."""


class SandboxExecutionError(SandboxError):
    """Raised when a validated calculation script encounters a runtime math/data error."""


class SandboxResult(BaseModel):
    ok: bool = True
    result: Any = None
    variables: dict[str, Any] = Field(default_factory=dict)
    stdout: str = ""
    duration_ms: int = 0


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted((_to_json_safe(v) for v in value), key=str)
    return str(value)


def _safe_pow(base: Any, exp: Any) -> Any:
    if isinstance(exp, (int, float)) and abs(exp) > MAX_POW_EXPONENT:
        raise SandboxSecurityError(
            f"Exponent {exp} exceeds sandbox limit (±{MAX_POW_EXPONENT})."
        )
    if (
        isinstance(base, Decimal)
        and isinstance(exp, (int, Decimal))
        and abs(exp) > MAX_POW_EXPONENT
    ):
        raise SandboxSecurityError(
            f"Exponent {exp} exceeds sandbox limit (±{MAX_POW_EXPONENT})."
        )
    return base**exp


def _safe_mult(left: Any, right: Any) -> Any:
    if isinstance(left, (str, bytes, list, tuple)) and isinstance(right, int):
        if len(left) * max(0, right) > MAX_SEQ_MULT_LEN:
            raise SandboxSecurityError("Sequence multiplication exceeds sandbox memory limit.")
    elif isinstance(right, (str, bytes, list, tuple)) and isinstance(left, int):
        if len(right) * max(0, left) > MAX_SEQ_MULT_LEN:
            raise SandboxSecurityError("Sequence multiplication exceeds sandbox memory limit.")
    return left * right


def _safe_range(*args: int) -> range:
    r = range(*args)
    if len(r) > MAX_RANGE_ITEMS:
        raise SandboxSecurityError(
            f"range() size {len(r)} exceeds sandbox limit ({MAX_RANGE_ITEMS})."
        )
    return r


def _restricted_import(
    name: str,
    globals_: dict[str, Any] | None = None,
    locals_: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    del globals_, locals_, fromlist
    if level != 0 or name not in ALLOWED_MODULES:
        raise SandboxSecurityError(f"Import of module '{name}' is blocked in sandbox.")
    module_map = {
        "math": math,
        "statistics": statistics,
        "datetime": _dt,
        "decimal": decimal,
        "json": json,
    }
    return module_map[name]


class _SandboxASTValidator(ast.NodeVisitor):
    """Strict AST validator allowing safe mathematical/date calculations only."""

    def visit(self, node: ast.AST) -> Any:
        if not isinstance(node, ALLOWED_AST_NODES):
            raise SandboxSecurityError(
                f"Disallowed Python syntax node: {type(node).__name__}"
            )
        return super().visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in ALLOWED_MODULES:
                raise SandboxSecurityError(
                    f"Import of '{alias.name}' is not allowed in sandbox."
                )
            bound_name = alias.asname or alias.name
            if bound_name.startswith("_") or bound_name in FORBIDDEN_NAMES:
                raise SandboxSecurityError(f"Disallowed import alias '{bound_name}'.")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0 or node.module not in ALLOWED_MODULES:
            raise SandboxSecurityError(
                f"Import from '{node.module}' is not allowed in sandbox."
            )
        for alias in node.names:
            if alias.name == "*":
                raise SandboxSecurityError("Wildcard imports are not allowed in sandbox.")
            if alias.name.startswith("_") or alias.name in FORBIDDEN_ATTRS:
                raise SandboxSecurityError(
                    f"Disallowed imported symbol '{alias.name}' from '{node.module}'."
                )
            bound_name = alias.asname or alias.name
            if bound_name.startswith("_") or bound_name in FORBIDDEN_NAMES:
                raise SandboxSecurityError(f"Disallowed import alias '{bound_name}'.")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") or node.id in FORBIDDEN_NAMES:
            raise SandboxSecurityError(f"Disallowed identifier '{node.id}' in sandbox.")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRS:
            raise SandboxSecurityError(f"Disallowed attribute '.{node.attr}' in sandbox.")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name.startswith("_") or node.name in FORBIDDEN_NAMES:
            raise SandboxSecurityError(f"Disallowed function name '{node.name}'.")
        if node.decorator_list:
            raise SandboxSecurityError("Function decorators are not allowed in sandbox.")
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            raise SandboxSecurityError("Bare 'except:' blocks are not allowed in sandbox.")
        if isinstance(node.type, ast.Name) and node.type.id in {"BaseException", "Exception"}:
            raise SandboxSecurityError(
                f"Catching '{node.type.id}' broadly is not allowed in sandbox."
            )
        self.generic_visit(node)


class _SandboxGuardTransformer(ast.NodeTransformer):
    """Wrap Pow and Mult operations with runtime resource guards."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            call = ast.Call(
                func=ast.Name(id="_sandbox_pow", ctx=ast.Load()),
                args=[node.left, node.right],
                keywords=[],
            )
            return ast.copy_location(call, node)
        if isinstance(node.op, ast.Mult):
            call = ast.Call(
                func=ast.Name(id="_sandbox_mult", ctx=ast.Load()),
                args=[node.left, node.right],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node


def validate_sandbox_ast(script: str) -> ast.Module:
    """Parse and AST-validate a Python calculation script without executing it."""
    if not isinstance(script, str) or not script.strip():
        raise SandboxSecurityError("Sandbox script must be a non-empty string.")
    if len(script) > MAX_SCRIPT_CHARS:
        raise SandboxSecurityError(
            f"Sandbox script exceeds max length ({MAX_SCRIPT_CHARS} chars)."
        )
    try:
        tree = ast.parse(script, mode="exec")
    except SyntaxError as exc:
        raise SandboxExecutionError(f"Syntax error in calculation script: {exc}") from exc

    _SandboxASTValidator().visit(tree)
    return tree


def run_sandbox_python(
    script: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 2.0,
    max_steps: int = 100_000,
) -> SandboxResult:
    """Execute an AST-validated calculation script under timeout and step/memory bounds."""
    start = time.perf_counter()
    tree = validate_sandbox_ast(script)

    transformed = _SandboxGuardTransformer().visit(tree)
    assert isinstance(transformed, ast.Module)
    if transformed.body and isinstance(transformed.body[-1], ast.Expr):
        last_expr = transformed.body[-1]
        transformed.body[-1] = ast.copy_location(
            ast.Assign(
                targets=[ast.Name(id="__sandbox_last_expr__", ctx=ast.Store())],
                value=last_expr.value,
            ),
            last_expr,
        )
    ast.fix_missing_locations(transformed)

    compiled = compile(transformed, filename="<sandbox>", mode="exec")

    stdout_buf = io.StringIO()

    def _safe_print(*args: Any, sep: str = " ", end: str = "\n") -> None:
        text = sep.join(str(a) for a in args) + end
        if stdout_buf.tell() + len(text) > 16_000:
            raise SandboxSecurityError("Sandbox stdout limit exceeded.")
        stdout_buf.write(text)

    safe_builtins: dict[str, Any] = {
        "__import__": _restricted_import,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "divmod": divmod,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "frozenset": frozenset,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "pow": _safe_pow,
        "range": _safe_range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "print": _safe_print,
        "ValueError": ValueError,
        "ZeroDivisionError": ZeroDivisionError,
        "KeyError": KeyError,
        "IndexError": IndexError,
        "TypeError": TypeError,
    }

    exec_ns: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "_sandbox_pow": _safe_pow,
        "_sandbox_mult": _safe_mult,
        "math": math,
        "statistics": statistics,
        "json": json,
        "decimal": decimal,
        "Decimal": Decimal,
        "datetime": _dt.datetime,
        "date": _dt.date,
        "timedelta": _dt.timedelta,
        "timezone": _dt.timezone,
    }

    input_keys: set[str] = set()
    if variables:
        for k, v in variables.items():
            clean_k = str(k)
            if clean_k.startswith("_") or clean_k in FORBIDDEN_NAMES:
                raise SandboxSecurityError(f"Disallowed variable name '{clean_k}'.")
            exec_ns[clean_k] = copy.deepcopy(v)
            input_keys.add(clean_k)

    baseline_keys = set(exec_ns.keys())
    steps = 0
    timed_out = False

    def _trace(frame: Any, event: str, arg: Any) -> Any:
        nonlocal steps, timed_out
        del arg
        if timed_out:
            raise SandboxTimeoutError("Sandbox execution timed out.")
        if frame.f_code.co_filename == "<sandbox>" and event in {"line", "call"}:
            steps += 1
            if steps > max_steps:
                timed_out = True
                raise SandboxTimeoutError(
                    f"Sandbox exceeded maximum execution steps ({max_steps})."
                )
            if (time.perf_counter() - start) > timeout_seconds:
                timed_out = True
                raise SandboxTimeoutError(
                    f"Sandbox execution exceeded {timeout_seconds:.2f}s timeout."
                )
        return _trace

    old_trace = sys.gettrace()
    sys.settrace(_trace)
    try:
        exec(compiled, exec_ns, exec_ns)
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxExecutionError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        sys.settrace(old_trace)

    elapsed_ms = int(round((time.perf_counter() - start) * 1000))

    user_vars: dict[str, Any] = {}
    for k, v in exec_ns.items():
        if k.startswith("_"):
            continue
        if k in baseline_keys and k not in input_keys:
            continue
        if callable(v) or isinstance(v, type(math)):
            continue
        user_vars[k] = _to_json_safe(v)

    if "result" in exec_ns:
        final_result = _to_json_safe(exec_ns["result"])
    elif "__sandbox_last_expr__" in exec_ns and exec_ns["__sandbox_last_expr__"] is not None:
        final_result = _to_json_safe(exec_ns["__sandbox_last_expr__"])
    elif "output" in exec_ns:
        final_result = _to_json_safe(exec_ns["output"])
    else:
        final_result = {k: v for k, v in user_vars.items() if k not in input_keys}

    return SandboxResult(
        ok=True,
        result=final_result,
        variables=user_vars,
        stdout=stdout_buf.getvalue(),
        duration_ms=elapsed_ms,
    )

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
ISOLATED_MODULES = {"inventory", "po", "mailroom", "planning", "agents"}


def _iter_module_py_files(module_name: str) -> list[Path]:
    mod_dir = BACKEND_ROOT / module_name
    if not mod_dir.exists():
        return []
    return sorted(mod_dir.rglob("*.py"))


def test_no_sideways_imports_between_domain_modules() -> None:
    violations: list[str] = []
    for mod in sorted(ISOLATED_MODULES):
        forbidden = ISOLATED_MODULES - {mod}
        for py_file in _iter_module_py_files(mod):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if len(parts) >= 2 and parts[0] == "backend" and parts[1] in forbidden:
                            violations.append(f"{py_file}: imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if len(parts) >= 2 and parts[0] == "backend" and parts[1] in forbidden:
                        violations.append(f"{py_file}: imports from {node.module}")
    assert not violations, "Sideways import violations:\n" + "\n".join(violations)


def test_no_wall_clock_datetime_calls_in_domain_logic() -> None:
    checked_modules = ISOLATED_MODULES | {"orchestrator"}
    violations: list[str] = []
    for mod in sorted(checked_modules):
        for py_file in _iter_module_py_files(mod):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    is_now_call = node.func.attr in {"now", "utcnow"}
                    if is_now_call and isinstance(node.func.value, ast.Name):
                        if node.func.value.id == "datetime":
                            violations.append(
                                f"{py_file}:{node.lineno} calls datetime.{node.func.attr}()"
                            )
    assert not violations, "Wall-clock datetime calls found:\n" + "\n".join(violations)

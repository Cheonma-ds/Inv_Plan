import time
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.config import settings
from backend.core.models import EvalRun


class EvalCaseSpec(BaseModel):
    id: str
    name: str
    task: str
    input: dict[str, Any] = Field(default_factory=dict)
    mock_response: dict[str, Any] | None = None
    expect: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class EvalSuiteSpec(BaseModel):
    suite_name: str
    agent_name: str
    description: str = ""
    cases: list[EvalCaseSpec] = Field(default_factory=list)


class EvalAssertionResult(BaseModel):
    field: str
    rule: str
    expected: Any = None
    actual: Any = None
    passed: bool
    message: str


class EvalCaseResult(BaseModel):
    id: str
    name: str
    task: str
    passed: bool
    reason: str
    latency_ms: int = 0
    assertions: list[EvalAssertionResult] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class EvalRunReport(BaseModel):
    id: int | None = None
    agent_name: str
    suite_name: str
    mode: str
    model_name: str
    total_cases: int
    passed_cases: int
    pass_rate: float
    results: list[EvalCaseResult] = Field(default_factory=list)
    timestamp: str


class AgentEvalExecutor(Protocol):
    """Agent-agnostic callback contract implemented by each agent's eval adapter."""

    def execute_eval_case(
        self,
        session: Session,
        *,
        task: str,
        case_input: dict[str, Any],
        mode: str,
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def load_eval_suite(suite_path: Path | str) -> EvalSuiteSpec:
    path = Path(suite_path)
    if not path.exists():
        raise FileNotFoundError(f"Eval suite YAML not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Eval suite YAML must be a mapping: {path}")
    return EvalSuiteSpec.model_validate(raw)


def _resolve_field_path(payload: dict[str, Any], field_path: str) -> tuple[bool, Any]:
    """Resolve dot-separated paths including numeric list indices (e.g. 'line_items.0.qty')."""
    current: Any = payload
    for part in field_path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list):
            if part == "length":
                current = len(current)
                continue
            try:
                idx = int(part)
            except ValueError:
                return False, None
            if idx < 0 or idx >= len(current):
                return False, None
            current = current[idx]
        else:
            return False, None
    return True, current


def _values_equal(actual: Any, expected: Any, tol: float = 0.01) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            return abs(float(actual) - float(expected)) <= tol
        return False
    return actual == expected


def _evaluate_rule(
    field_name: str,
    rule_name: str,
    expected: Any,
    found: bool,
    actual: Any,
) -> EvalAssertionResult:
    if not found:
        return EvalAssertionResult(
            field=field_name,
            rule=rule_name,
            expected=expected,
            actual=None,
            passed=False,
            message=f"{field_name}: missing from output",
        )

    if rule_name == "equals":
        ok = _values_equal(actual, expected)
        msg = (
            f"{field_name} == {expected!r}"
            if ok
            else f"{field_name}: expected {expected!r}, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="equals",
            expected=expected,
            actual=actual,
            passed=ok,
            message=msg,
        )

    if rule_name == "min":
        is_num = isinstance(actual, (int, float)) and not isinstance(actual, bool)
        ok = is_num and actual >= expected
        msg = (
            f"{field_name} >= {expected}"
            if ok
            else f"{field_name}: expected >= {expected}, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name, rule="min", expected=expected, actual=actual, passed=ok, message=msg
        )

    if rule_name == "max":
        is_num = isinstance(actual, (int, float)) and not isinstance(actual, bool)
        ok = is_num and actual <= expected
        msg = (
            f"{field_name} <= {expected}"
            if ok
            else f"{field_name}: expected <= {expected}, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name, rule="max", expected=expected, actual=actual, passed=ok, message=msg
        )

    if rule_name == "in":
        allowed = expected if isinstance(expected, list) else [expected]
        ok = actual in allowed
        msg = (
            f"{field_name} in {allowed!r}"
            if ok
            else f"{field_name}: expected one of {allowed!r}, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name, rule="in", expected=allowed, actual=actual, passed=ok, message=msg
        )

    if rule_name == "contains":
        if isinstance(actual, list):
            ok = expected in actual
        elif isinstance(actual, str):
            ok = str(expected).lower() in actual.lower()
        else:
            ok = False
        msg = (
            f"{field_name} contains {expected!r}"
            if ok
            else f"{field_name}: expected to contain {expected!r}, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="contains",
            expected=expected,
            actual=actual,
            passed=ok,
            message=msg,
        )

    if rule_name == "not_contains":
        if isinstance(actual, list):
            ok = expected not in actual
        elif isinstance(actual, str):
            ok = str(expected).lower() not in actual.lower()
        else:
            ok = False
        msg = (
            f"{field_name} does not contain {expected!r}"
            if ok
            else f"{field_name}: must not contain {expected!r}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="not_contains",
            expected=expected,
            actual=actual,
            passed=ok,
            message=msg,
        )

    if rule_name == "not_contains_any_ci":
        phrases = [str(p) for p in (expected if isinstance(expected, list) else [expected])]
        haystack = str(actual or "").lower()
        hits = [p for p in phrases if p.lower() in haystack]
        ok = len(hits) == 0
        msg = (
            f"{field_name} avoids forbidden phrases"
            if ok
            else f"{field_name}: matched forbidden phrase(s): {', '.join(hits)}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="not_contains_any_ci",
            expected=phrases,
            actual=hits or None,
            passed=ok,
            message=msg,
        )

    if rule_name == "contains_any_ci":
        phrases = [str(p) for p in (expected if isinstance(expected, list) else [expected])]
        haystack = str(actual or "").lower()
        ok = any(p.lower() in haystack for p in phrases)
        msg = (
            f"{field_name} includes expected concept"
            if ok
            else f"{field_name}: expected one of {phrases!r} in {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="contains_any_ci",
            expected=phrases,
            actual=actual,
            passed=ok,
            message=msg,
        )

    if rule_name == "min_len":
        ok = hasattr(actual, "__len__") and len(actual) >= int(expected)
        actual_len = len(actual) if hasattr(actual, "__len__") else None
        msg = (
            f"{field_name} length >= {expected}"
            if ok
            else f"{field_name}: expected length >= {expected}, got {actual_len}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="min_len",
            expected=expected,
            actual=actual_len,
            passed=ok,
            message=msg,
        )

    if rule_name == "not_null":
        want_non_null = bool(expected)
        is_non_null = actual is not None and (not isinstance(actual, str) or actual.strip() != "")
        ok = is_non_null if want_non_null else not is_non_null
        msg = (
            f"{field_name} is present"
            if ok
            else f"{field_name}: expected non-empty value, got {actual!r}"
        )
        return EvalAssertionResult(
            field=field_name,
            rule="not_null",
            expected=expected,
            actual=actual,
            passed=ok,
            message=msg,
        )

    return EvalAssertionResult(
        field=field_name,
        rule=rule_name,
        expected=expected,
        actual=actual,
        passed=False,
        message=f"{field_name}: unsupported assertion rule '{rule_name}'",
    )


def evaluate_expectations(
    output: dict[str, Any],
    expect: dict[str, Any],
) -> list[EvalAssertionResult]:
    results: list[EvalAssertionResult] = []
    for key, spec in expect.items():
        # Support DESIGN.md §8 shorthands: min_confidence / max_confidence
        if key == "min_confidence":
            found, actual = _resolve_field_path(output, "confidence")
            results.append(_evaluate_rule("confidence", "min", spec, found, actual))
            continue
        if key == "max_confidence":
            found, actual = _resolve_field_path(output, "confidence")
            results.append(_evaluate_rule("confidence", "max", spec, found, actual))
            continue

        found, actual = _resolve_field_path(output, key)
        if isinstance(spec, dict):
            if "approx" in spec:
                tol = float(spec.get("tol", 0.01))
                ok = found and _values_equal(actual, spec["approx"], tol=tol)
                msg = (
                    f"{key} ≈ {spec['approx']} (±{tol})"
                    if ok
                    else f"{key}: expected {spec['approx']} (±{tol}), got {actual!r}"
                )
                results.append(
                    EvalAssertionResult(
                        field=key,
                        rule="approx",
                        expected=spec["approx"],
                        actual=actual,
                        passed=ok,
                        message=msg,
                    )
                )
            for rule_name, rule_val in spec.items():
                if rule_name in {"approx", "tol"}:
                    continue
                results.append(_evaluate_rule(key, rule_name, rule_val, found, actual))
        else:
            results.append(_evaluate_rule(key, "equals", spec, found, actual))
    return results


def run_eval_suite(
    session: Session,
    *,
    agent_name: str,
    suite_path: Path | str,
    executor: AgentEvalExecutor,
    mode: str = "offline",
    model_name: str | None = None,
) -> EvalRunReport:
    if mode not in {"offline", "live"}:
        raise ValueError(f"Unsupported eval mode '{mode}'. Use 'offline' or 'live'.")

    suite = load_eval_suite(suite_path)
    if suite.agent_name != agent_name:
        raise ValueError(
            f"Eval suite '{suite.suite_name}' targets agent '{suite.agent_name}', "
            f"not '{agent_name}'."
        )

    effective_model = model_name or (
        settings.gemini_model if mode == "live" else f"{settings.gemini_model} (offline)"
    )

    case_results: list[EvalCaseResult] = []
    for case in suite.cases:
        start = time.perf_counter()
        try:
            output = executor.execute_eval_case(
                session,
                task=case.task,
                case_input=case.input,
                mode=mode,
                mock_response=case.mock_response,
            )
            elapsed_ms = int(round((time.perf_counter() - start) * 1000))
            assertions = evaluate_expectations(output, case.expect)
            passed = all(a.passed for a in assertions)
            if passed:
                reason = f"All {len(assertions)} check(s) passed."
            else:
                failed_msgs = [a.message for a in assertions if not a.passed]
                reason = "; ".join(failed_msgs)
            case_results.append(
                EvalCaseResult(
                    id=case.id,
                    name=case.name,
                    task=case.task,
                    passed=passed,
                    reason=reason,
                    latency_ms=elapsed_ms,
                    assertions=assertions,
                    output=output,
                )
            )
        except Exception as exc:
            elapsed_ms = int(round((time.perf_counter() - start) * 1000))
            case_results.append(
                EvalCaseResult(
                    id=case.id,
                    name=case.name,
                    task=case.task,
                    passed=False,
                    reason=f"Execution error: {exc}",
                    latency_ms=elapsed_ms,
                    assertions=[],
                    output={},
                    error=str(exc),
                )
            )

    total = len(case_results)
    passed_count = sum(1 for c in case_results if c.passed)
    pass_rate = round((passed_count / total) * 100.0, 1) if total > 0 else 0.0
    sim_now = clock.now(session)

    stored_results = [
        {
            **c.model_dump(mode="json"),
            "mode": mode,
        }
        for c in case_results
    ]
    eval_row = EvalRun(
        agent_name=agent_name,
        suite_name=suite.suite_name,
        total_cases=total,
        passed_cases=passed_count,
        pass_rate=pass_rate,
        results_json=stored_results,
        model_name=effective_model,
        timestamp=sim_now,
    )
    session.add(eval_row)
    session.flush()

    return EvalRunReport(
        id=eval_row.id,
        agent_name=agent_name,
        suite_name=suite.suite_name,
        mode=mode,
        model_name=effective_model,
        total_cases=total,
        passed_cases=passed_count,
        pass_rate=pass_rate,
        results=case_results,
        timestamp=clock.ensure_utc(sim_now).isoformat(),
    )


def list_eval_runs(
    session: Session,
    *,
    agent_name: str | None = None,
    limit: int = 20,
) -> list[EvalRun]:
    stmt = select(EvalRun)
    if agent_name:
        stmt = stmt.where(EvalRun.agent_name == agent_name)
    stmt = stmt.order_by(EvalRun.timestamp.desc(), EvalRun.id.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def serialize_eval_run(row: EvalRun) -> dict[str, Any]:
    raw_results = row.results_json or []
    inferred_mode = "offline"
    if raw_results and isinstance(raw_results[0], dict) and "mode" in raw_results[0]:
        inferred_mode = str(raw_results[0]["mode"])
    elif "(offline)" not in row.model_name:
        inferred_mode = "live"

    return {
        "id": row.id,
        "agent_name": row.agent_name,
        "suite_name": row.suite_name,
        "mode": inferred_mode,
        "model_name": row.model_name,
        "total_cases": row.total_cases,
        "passed_cases": row.passed_cases,
        "pass_rate": row.pass_rate,
        "results": raw_results,
        "timestamp": clock.ensure_utc(row.timestamp).isoformat(),
    }

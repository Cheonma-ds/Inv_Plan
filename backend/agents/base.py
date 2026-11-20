from abc import ABC


class BaseAgent(ABC):
    """Agent-agnostic base contract for all InvPlan agents (Lex today, Copilot tomorrow)."""

    agent_name: str
    eval_suite_path: str | None = None

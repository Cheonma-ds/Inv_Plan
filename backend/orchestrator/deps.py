from backend.orchestrator.workflows import WorkflowEngine

_default_engine = WorkflowEngine()


def get_workflow_engine() -> WorkflowEngine:
    return _default_engine

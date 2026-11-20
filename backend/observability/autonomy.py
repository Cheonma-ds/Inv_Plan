from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import ApprovalOutcome, AutonomyMode
from backend.core.models import AutonomyPolicy


def record_approval_outcome(
    session: Session,
    *,
    agent_name: str,
    action_category: str,
    outcome: ApprovalOutcome,
) -> AutonomyPolicy:
    policy = session.scalar(
        select(AutonomyPolicy).where(
            AutonomyPolicy.agent_name == agent_name,
            AutonomyPolicy.action_category == action_category,
        )
    )
    sim_now = clock.now(session)
    if policy is None:
        policy = AutonomyPolicy(
            agent_name=agent_name,
            action_category=action_category,
            mode=AutonomyMode.APPROVAL_REQUIRED.value,
            approved_as_is_count=0,
            approved_edited_count=0,
            rejected_count=0,
            updated_at=sim_now,
        )
        session.add(policy)

    if outcome == ApprovalOutcome.APPROVED_AS_IS:
        policy.approved_as_is_count += 1
    elif outcome == ApprovalOutcome.APPROVED_EDITED:
        policy.approved_edited_count += 1
    elif outcome == ApprovalOutcome.REJECTED:
        policy.rejected_count += 1

    policy.updated_at = sim_now
    session.flush()
    return policy


def list_autonomy_policies(
    session: Session,
    *,
    agent_name: str | None = None,
) -> list[AutonomyPolicy]:
    stmt = select(AutonomyPolicy)
    if agent_name:
        stmt = stmt.where(AutonomyPolicy.agent_name == agent_name)
    stmt = stmt.order_by(AutonomyPolicy.agent_name.asc(), AutonomyPolicy.action_category.asc())
    return list(session.scalars(stmt).all())

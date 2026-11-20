from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.enums import Criticality, PersonaKey, POStatus
from backend.core.models import Part, Supplier
from backend.po import (
    ALLOWED_TRANSITIONS,
    InvalidPOTransitionError,
    can_transition,
    create_po,
    transition_po,
)


def _make_po(session: Session, status: POStatus = POStatus.PENDING_DISPATCH_APPROVAL):
    sup = Supplier(
        persona_key=PersonaKey.SUP1.value,
        name="Apex",
        email="orders@apex.in",
    )
    part = Part(
        sku="SM-001",
        name="State Machine Part",
        criticality=Criticality.STANDARD.value,
        on_hand_stock=5,
        rop=3,
        replenishment_point=10,
    )
    session.add_all([sup, part])
    session.flush()
    return create_po(
        session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=5,
        unit_price=250.0,
        initial_status=status,
    )


def test_every_allowed_and_forbidden_transition(db_session: Session) -> None:
    po = _make_po(db_session)
    for src in POStatus:
        for dst in POStatus:
            po.status = src.value
            db_session.flush()
            expected = dst in ALLOWED_TRANSITIONS[src]
            assert can_transition(src, dst) is expected
            if expected:
                transition_po(db_session, po, dst)
                assert po.status == dst.value
            else:
                with pytest.raises(InvalidPOTransitionError):
                    transition_po(db_session, po, dst)
                assert po.status == src.value


def test_promised_eta_is_immutable_while_revised_eta_updates(db_session: Session) -> None:
    po = _make_po(db_session)
    sim_now = clock.now(db_session)

    transition_po(db_session, po, POStatus.SENT_AWAITING_ACK)
    assert po.sent_at is not None
    assert clock.ensure_utc(po.sent_at) == sim_now

    first_eta = sim_now + timedelta(days=10)
    transition_po(db_session, po, POStatus.ACKNOWLEDGED, promised_eta=first_eta)
    assert clock.ensure_utc(po.promised_eta) == first_eta
    assert po.revised_eta is None

    # Supplier later reports a delay: promised_eta MUST stay unchanged; revised_eta updates.
    delayed_eta = sim_now + timedelta(days=17)
    transition_po(
        db_session,
        po,
        POStatus.ACKNOWLEDGED,
        promised_eta=delayed_eta,
        revised_eta=delayed_eta,
        note="Non-critical minor delay accepted",
    )
    assert clock.ensure_utc(po.promised_eta) == first_eta
    assert clock.ensure_utc(po.revised_eta) == delayed_eta
    assert "Non-critical minor delay accepted" in po.notes

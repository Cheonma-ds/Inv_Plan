from datetime import timedelta

from sqlalchemy import select

from backend.core import clock
from backend.core.enums import DispatchDetailsStatus, POStatus
from backend.core.models import Part, Supplier
from backend.po import (
    apply_po_logistics_updates,
    compute_missing_dispatch_fields,
    create_po,
    transition_po,
)


def test_state_machine_sets_acknowledged_at_and_closed_at(seeded_session):
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    sup = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup1"))
    assert part is not None and sup is not None

    po = create_po(
        seeded_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=4,
        unit_price=1200.0,
    )
    assert po.sent_at is None
    assert po.acknowledged_at is None
    assert po.closed_at is None

    clock.advance(seeded_session, hours=2)
    transition_po(seeded_session, po, POStatus.SENT_AWAITING_ACK)
    assert po.sent_at is not None
    assert po.sent_at > po.created_at

    clock.advance(seeded_session, hours=4)
    promised_eta = clock.now(seeded_session) + timedelta(days=10)
    transition_po(seeded_session, po, POStatus.ACKNOWLEDGED, promised_eta=promised_eta)
    assert po.acknowledged_at == clock.now(seeded_session)

    transition_po(seeded_session, po, POStatus.RECEIVED_PENDING_INVOICE)
    clock.advance(seeded_session, hours=1)
    transition_po(seeded_session, po, POStatus.CLOSED)
    assert po.closed_at == clock.now(seeded_session)


def test_apply_po_logistics_updates_missing_deferred_and_complete(seeded_session):
    part = seeded_session.scalar(select(Part).where(Part.sku == "BRG-402"))
    sup = seeded_session.scalar(select(Supplier).where(Supplier.persona_key == "sup1"))
    assert part is not None and sup is not None

    po = create_po(
        seeded_session,
        part_id=part.id,
        supplier_id=sup.id,
        qty=5,
        unit_price=1200.0,
        initial_status=POStatus.ACKNOWLEDGED,
    )
    assert compute_missing_dispatch_fields(po) == [
        "promised_dispatch_date",
        "carrier_name",
        "tracking_number",
    ]

    sim_now = clock.now(seeded_session)
    # 1. Partial update where supplier promises tracking later -> DEFERRED_2D
    res1 = apply_po_logistics_updates(
        seeded_session,
        po,
        promised_dispatch_date=sim_now + timedelta(days=2),
        details_promised_later=True,
    )
    assert "promised_dispatch_date" in res1.changed_fields
    assert res1.missing_fields == ["carrier_name", "tracking_number"]
    assert res1.dispatch_details_status == DispatchDetailsStatus.DEFERRED_2D.value
    assert res1.dispatch_details_due_at == sim_now + timedelta(days=2)

    # 2. Follow-up email adds carrier only without deferring -> AWAITING_IMMEDIATE
    res2 = apply_po_logistics_updates(
        seeded_session,
        po,
        carrier_name="BlueDart",
        details_promised_later=False,
    )
    assert res2.changed_fields == {"carrier_name": (None, "BlueDart")}
    assert res2.missing_fields == ["tracking_number"]
    assert res2.dispatch_details_status == DispatchDetailsStatus.AWAITING_IMMEDIATE.value
    assert res2.dispatch_details_due_at is None

    # 3. Final update supplies actual_dispatch_date and tracking_number -> COMPLETE
    res3 = apply_po_logistics_updates(
        seeded_session,
        po,
        actual_dispatch_date=sim_now + timedelta(days=2),
        tracking_number="BD-994821",
    )
    assert res3.missing_fields == []
    assert res3.dispatch_details_status == DispatchDetailsStatus.COMPLETE.value
    assert res3.dispatch_details_due_at is None
    assert po.tracking_number == "BD-994821"

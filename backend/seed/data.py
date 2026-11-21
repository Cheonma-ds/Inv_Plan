import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import Base
from backend.core.enums import (
    AutonomyMode,
    Criticality,
    DispatchDetailsStatus,
    EmailCategory,
    GRNStatus,
    InventoryTxType,
    InvoiceMatchStatus,
    PersonaKey,
    POStatus,
    SupplierMemorySource,
)
from backend.core.models import (
    AutonomyPolicy,
    GoodsReceipt,
    InventoryTransaction,
    Invoice,
    Part,
    PartSupplierTerm,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)

SEED_START_TIME = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PartSpec:
    sku: str
    name: str
    criticality: Criticality
    on_hand: int
    rop: int
    replenishment_point: int
    target_coverage_days: int
    primary_key: str
    primary_price: float
    primary_quoted_lt: int
    primary_effective_lt: float
    primary_lt_std: float
    primary_moq: int
    backup_key: str
    backup_price: float
    backup_quoted_lt: int
    backup_effective_lt: float
    backup_lt_std: float
    backup_moq: int
    monthly_demand_mean: float
    intermittent: bool = False


PART_SPECS: tuple[PartSpec, ...] = (
    # CRITICAL (5 parts)
    PartSpec(
        sku="BRG-402",
        name="Deep Groove Ball Bearing 6208",
        criticality=Criticality.CRITICAL,
        on_hand=4,
        rop=3,
        replenishment_point=6,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=450.0,
        primary_quoted_lt=10,
        primary_effective_lt=10.2,
        primary_lt_std=1.0,
        primary_moq=1,
        backup_key=PersonaKey.SUP2.value,
        backup_price=480.0,
        backup_quoted_lt=15,
        backup_effective_lt=23.5,
        backup_lt_std=4.2,
        backup_moq=2,
        monthly_demand_mean=6.0,
    ),
    PartSpec(
        sku="VLV-204",
        name="Hydraulic Servo Solenoid Valve HV-40",
        criticality=Criticality.CRITICAL,
        on_hand=5,
        rop=4,
        replenishment_point=12,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=1500.0,
        primary_quoted_lt=15,
        primary_effective_lt=23.5,
        primary_lt_std=4.0,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=1620.0,
        backup_quoted_lt=12,
        backup_effective_lt=12.0,
        backup_lt_std=1.1,
        backup_moq=2,
        monthly_demand_mean=9.0,
    ),
    PartSpec(
        sku="SNS-802",
        name="Proximity Inductive Sensor M18",
        criticality=Criticality.CRITICAL,
        on_hand=4,
        rop=5,
        replenishment_point=12,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=600.0,
        primary_quoted_lt=14,
        primary_effective_lt=16.5,
        primary_lt_std=3.2,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=640.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.2,
        backup_lt_std=1.0,
        backup_moq=2,
        monthly_demand_mean=11.0,
    ),
    PartSpec(
        sku="PMP-701",
        name="High-Pressure Coolant Pump",
        criticality=Criticality.CRITICAL,
        on_hand=3,
        rop=4,
        replenishment_point=10,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=2000.0,
        primary_quoted_lt=16,
        primary_effective_lt=18.5,
        primary_lt_std=3.4,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=2150.0,
        backup_quoted_lt=12,
        backup_effective_lt=12.1,
        backup_lt_std=1.0,
        backup_moq=2,
        monthly_demand_mean=8.0,
    ),
    PartSpec(
        sku="BRG-405",
        name="Taper Roller Bearing 30208",
        criticality=Criticality.CRITICAL,
        on_hand=5,
        rop=4,
        replenishment_point=10,
        target_coverage_days=45,
        primary_key=PersonaKey.SUP3.value,
        primary_price=1850.0,
        primary_quoted_lt=18,
        primary_effective_lt=20.0,
        primary_lt_std=3.5,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=1950.0,
        backup_quoted_lt=14,
        backup_effective_lt=14.0,
        backup_lt_std=1.0,
        backup_moq=2,
        monthly_demand_mean=2.0,
        intermittent=True,
    ),
    # STANDARD (6 parts)
    PartSpec(
        sku="FLT-108",
        name="Hydraulic Return Line Filter",
        criticality=Criticality.STANDARD,
        on_hand=18,
        rop=12,
        replenishment_point=30,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=320.0,
        primary_quoted_lt=10,
        primary_effective_lt=10.1,
        primary_lt_std=0.9,
        primary_moq=5,
        backup_key=PersonaKey.SUP2.value,
        backup_price=305.0,
        backup_quoted_lt=15,
        backup_effective_lt=23.0,
        backup_lt_std=3.8,
        backup_moq=5,
        monthly_demand_mean=18.0,
    ),
    PartSpec(
        sku="BLT-301",
        name="M12 Alloy Hex Bolt Pack (100pc)",
        criticality=Criticality.STANDARD,
        on_hand=15,
        rop=10,
        replenishment_point=30,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=100.0,
        primary_quoted_lt=15,
        primary_effective_lt=23.0,
        primary_lt_std=4.1,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=108.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.2,
        backup_lt_std=1.0,
        backup_moq=10,
        monthly_demand_mean=25.0,
    ),
    PartSpec(
        sku="GSK-512",
        name="Viton High-Temp Gasket",
        criticality=Criticality.STANDARD,
        on_hand=20,
        rop=12,
        replenishment_point=35,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=200.0,
        primary_quoted_lt=15,
        primary_effective_lt=23.2,
        primary_lt_std=4.0,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=215.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.0,
        backup_lt_std=0.8,
        backup_moq=10,
        monthly_demand_mean=24.0,
    ),
    PartSpec(
        sku="SHF-605",
        name="Precision Linear Shaft 25mm",
        criticality=Criticality.STANDARD,
        on_hand=14,
        rop=10,
        replenishment_point=25,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=800.0,
        primary_quoted_lt=15,
        primary_effective_lt=22.5,
        primary_lt_std=3.9,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=860.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.1,
        backup_lt_std=1.0,
        backup_moq=5,
        monthly_demand_mean=15.0,
    ),
    PartSpec(
        sku="RLY-901",
        name="PLC Safety Relay Module 24V",
        criticality=Criticality.STANDARD,
        on_hand=8,
        rop=5,
        replenishment_point=15,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=100.0,
        primary_quoted_lt=14,
        primary_effective_lt=16.0,
        primary_lt_std=3.0,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=108.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.1,
        backup_lt_std=0.9,
        backup_moq=5,
        monthly_demand_mean=15.0,
    ),
    PartSpec(
        sku="BLT-305",
        name="Timing Belt HTD-8M-1200",
        criticality=Criticality.STANDARD,
        on_hand=16,
        rop=12,
        replenishment_point=30,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=640.0,
        primary_quoted_lt=10,
        primary_effective_lt=10.2,
        primary_lt_std=0.9,
        primary_moq=5,
        backup_key=PersonaKey.SUP3.value,
        backup_price=620.0,
        backup_quoted_lt=14,
        backup_effective_lt=16.2,
        backup_lt_std=3.1,
        backup_moq=5,
        monthly_demand_mean=19.0,
    ),
    # LOW (4 parts)
    PartSpec(
        sku="WSH-104",
        name="Stainless Flat Washer M12",
        criticality=Criticality.LOW,
        on_hand=120,
        rop=40,
        replenishment_point=100,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=45.0,
        primary_quoted_lt=8,
        primary_effective_lt=8.0,
        primary_lt_std=0.7,
        primary_moq=25,
        backup_key=PersonaKey.SUP3.value,
        backup_price=42.0,
        backup_quoted_lt=12,
        backup_effective_lt=14.0,
        backup_lt_std=2.5,
        backup_moq=25,
        monthly_demand_mean=30.0,
    ),
    PartSpec(
        sku="ORG-112",
        name="Silicone O-Ring Assortment Box",
        criticality=Criticality.LOW,
        on_hand=30,
        rop=20,
        replenishment_point=50,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=240.0,
        primary_quoted_lt=15,
        primary_effective_lt=15.0,
        primary_lt_std=0.0,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=260.0,
        backup_quoted_lt=10,
        backup_effective_lt=10.2,
        backup_lt_std=0.9,
        backup_moq=10,
        monthly_demand_mean=28.0,
    ),
    PartSpec(
        sku="CBL-114",
        name="Brass Cable Gland PG13.5",
        criticality=Criticality.LOW,
        on_hand=60,
        rop=40,
        replenishment_point=100,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=85.0,
        primary_quoted_lt=10,
        primary_effective_lt=12.0,
        primary_lt_std=2.4,
        primary_moq=25,
        backup_key=PersonaKey.SUP1.value,
        backup_price=92.0,
        backup_quoted_lt=8,
        backup_effective_lt=8.2,
        backup_lt_std=0.8,
        backup_moq=25,
        monthly_demand_mean=55.0,
    ),
    PartSpec(
        sku="FUS-115",
        name="Panel Mount Fuse Holder 10A",
        criticality=Criticality.LOW,
        on_hand=15,
        rop=10,
        replenishment_point=24,
        target_coverage_days=45,
        primary_key=PersonaKey.SUP1.value,
        primary_price=190.0,
        primary_quoted_lt=9,
        primary_effective_lt=9.1,
        primary_lt_std=0.8,
        primary_moq=10,
        backup_key=PersonaKey.SUP2.value,
        backup_price=180.0,
        backup_quoted_lt=15,
        backup_effective_lt=22.0,
        backup_lt_std=3.6,
        backup_moq=10,
        monthly_demand_mean=4.0,
        intermittent=True,
    ),
)


def seed_database(session: Session, *, reset: bool = False) -> dict[str, int]:
    """Populate a fresh database with scenario-aligned 12-month demo baseline data."""
    if reset:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.flush()
        session.expunge_all()

    rng = random.Random(42)
    sim_now = clock.set_time(session, SEED_START_TIME)

    # 1. Seed 3 suppliers (Sup1 reliable, Sup2 chronically late & invoice-prone, Sup3 unresponsive)
    suppliers_by_key: dict[str, Supplier] = {}
    supplier_defs = [
        (
            PersonaKey.SUP1.value,
            "Apex Precision Components",
            "orders@apexprecision.in",
            "+91-80-4112-8801",
            94.0,
            0.92,
            1.00,
            0.92,
            "Reliable primary partner; consistently honors quoted lead times and accurate billing.",
        ),
        (
            PersonaKey.SUP2.value,
            "Bharat Industrial Fasteners",
            "sales@bharatfasteners.in",
            "+91-20-6624-3190",
            56.0,
            0.17,
            0.67,
            0.83,
            "Chronically delivers 7-10 days past quoted 15d lead time; verify unit prices on PDFs.",
        ),
        (
            PersonaKey.SUP3.value,
            "Deccan Electro-Spares",
            "dispatch@deccanelectro.in",
            "+91-40-2378-4412",
            63.0,
            0.67,
            0.92,
            0.42,
            (
                "Slow to acknowledge POs (frequently misses 48h SLA) "
                "and prone to mid-quarter price hikes."
            ),
        ),
    ]

    for persona_key, name, email, phone, rel, otd, inv_acc, resp, mem_note in supplier_defs:
        sup = Supplier(
            persona_key=persona_key,
            name=name,
            email=email,
            phone=phone,
            reliability_score=rel,
            otd_rate=otd,
            invoice_accuracy_rate=inv_acc,
            response_rate=resp,
        )
        session.add(sup)
        session.flush()
        suppliers_by_key[persona_key] = sup

        session.add(
            SupplierMemoryNote(
                supplier_id=sup.id,
                note=mem_note,
                source=SupplierMemorySource.MANAGER_ADDED.value,
                created_at=sim_now - timedelta(days=45),
                updated_at=sim_now - timedelta(days=45),
            )
        )

    # 2. Seed 15 parts + Primary & Backup PartSupplierTerm rows + 12m consumption ledger
    parts_by_sku: dict[str, Part] = {}
    tx_count = 0
    for spec in PART_SPECS:
        part = Part(
            sku=spec.sku,
            name=spec.name,
            criticality=spec.criticality.value,
            on_hand_stock=spec.on_hand,
            quarantined_stock=0,
            rop=spec.rop,
            replenishment_point=spec.replenishment_point,
            target_coverage_days=spec.target_coverage_days,
            manual_demand_override=None,
        )
        session.add(part)
        session.flush()
        parts_by_sku[spec.sku] = part

        primary_sup = suppliers_by_key[spec.primary_key]
        backup_sup = suppliers_by_key[spec.backup_key]
        validated_at = sim_now - timedelta(days=30)

        session.add_all(
            [
                PartSupplierTerm(
                    part_id=part.id,
                    supplier_id=primary_sup.id,
                    supplier_rank=1,
                    agreed_unit_price=spec.primary_price,
                    currency="INR",
                    quoted_lead_time_days=spec.primary_quoted_lt,
                    effective_lead_time_days=spec.primary_effective_lt,
                    lead_time_std_dev=spec.primary_lt_std,
                    moq=spec.primary_moq,
                    last_validated_at=validated_at,
                ),
                PartSupplierTerm(
                    part_id=part.id,
                    supplier_id=backup_sup.id,
                    supplier_rank=2,
                    agreed_unit_price=spec.backup_price,
                    currency="INR",
                    quoted_lead_time_days=spec.backup_quoted_lt,
                    effective_lead_time_days=spec.backup_effective_lt,
                    lead_time_std_dev=spec.backup_lt_std,
                    moq=spec.backup_moq,
                    last_validated_at=validated_at,
                ),
            ]
        )

        # Generate 12 months of consumption transactions (buckets 12..1 months ago)
        for months_ago in range(12, 0, -1):
            if spec.intermittent and months_ago not in (11, 8, 5, 2):
                continue

            std_dev = max(1.0, spec.monthly_demand_mean * 0.2)
            monthly_qty = max(1, int(round(rng.gauss(spec.monthly_demand_mean, std_dev))))
            first_pull = max(1, monthly_qty // 2)
            second_pull = monthly_qty - first_pull
            base_day = sim_now - timedelta(days=months_ago * 30 - 5)

            session.add(
                InventoryTransaction(
                    part_id=part.id,
                    type=InventoryTxType.CONSUMPTION.value,
                    qty_delta=-first_pull,
                    timestamp=base_day,
                    note="Historical engineering withdrawal (seeded)",
                )
            )
            tx_count += 1
            if second_pull > 0:
                session.add(
                    InventoryTransaction(
                        part_id=part.id,
                        type=InventoryTxType.CONSUMPTION.value,
                        qty_delta=-second_pull,
                        timestamp=base_day + timedelta(days=12),
                        note="Historical production withdrawal (seeded)",
                    )
                )
                tx_count += 1

    # 3. Seed 36 closed historical POs (12 per supplier) reflecting true supplier profiles
    # Note: ORG-112 intentionally has 0 closed POs (< 3) to preserve cold-start LT fallback.
    po_counter = 1001
    historical_po_plan: list[tuple[str, list[str]]] = [
        (PersonaKey.SUP1.value, ["BRG-402", "FLT-108", "BLT-305", "WSH-104"]),
        (PersonaKey.SUP2.value, ["VLV-204", "BLT-301", "GSK-512", "SHF-605"]),
        (PersonaKey.SUP3.value, ["SNS-802", "PMP-701", "RLY-901", "CBL-114"]),
    ]

    closed_po_count = 0
    for sup_key, sku_pool in historical_po_plan:
        sup = suppliers_by_key[sup_key]
        for idx in range(12):
            sku = sku_pool[idx % len(sku_pool)]
            part = parts_by_sku[sku]
            spec = next(s for s in PART_SPECS if s.sku == sku)

            days_ago_created = 345 - idx * 26
            created_at = sim_now - timedelta(days=days_ago_created)
            sent_at = created_at + timedelta(hours=2)
            quoted_lt = spec.primary_quoted_lt
            promised_eta = sent_at + timedelta(days=quoted_lt)

            if sup_key == PersonaKey.SUP1.value:
                actual_delta = 0 if idx < 11 else 2  # 11/12 on time
                follow_ups = 0 if idx < 11 else 1
                had_invoice_mismatch = False
            elif sup_key == PersonaKey.SUP2.value:
                actual_delta = 0 if idx in (1, 7) else rng.randint(6, 11)  # 10/12 late (+6..+11d)
                follow_ups = 0 if idx < 10 else 1
                had_invoice_mismatch = idx in (2, 5, 8, 11)  # 4/12 mismatched v1 invoice
            else:
                actual_delta = 0 if idx < 8 else rng.randint(3, 6)  # 8/12 on time
                follow_ups = 0 if idx < 5 else rng.randint(1, 2)  # 7/12 needed nudges
                had_invoice_mismatch = idx == 9  # 1/12 mismatched v1 invoice

            delivered_at = promised_eta + timedelta(days=actual_delta)
            ack_delay_hours = 6 if sup_key == PersonaKey.SUP1.value else 18
            acknowledged_at = sent_at + timedelta(hours=ack_delay_hours)
            promised_dispatch = promised_eta - timedelta(days=3)
            actual_dispatch = delivered_at - timedelta(days=3)
            carrier_options = ("BlueDart", "Delhivery", "DTDC")
            carrier_name = carrier_options[idx % len(carrier_options)]
            carrier_prefix = {"BlueDart": "BD", "Delhivery": "DLV", "DTDC": "DT"}[carrier_name]
            order_qty = max(spec.primary_moq, spec.replenishment_point - spec.rop)
            po_num = f"PO-{po_counter}"
            tracking_number = f"{carrier_prefix}-{po_counter * 97}"
            po_counter += 1

            po = PurchaseOrder(
                po_number=po_num,
                part_id=part.id,
                supplier_id=sup.id,
                qty=order_qty,
                unit_price=spec.primary_price,
                status=POStatus.CLOSED.value,
                notes="Historical completed PO (seeded).",
                created_at=created_at,
                sent_at=sent_at,
                acknowledged_at=acknowledged_at,
                promised_dispatch_date=promised_dispatch,
                actual_dispatch_date=actual_dispatch,
                carrier_name=carrier_name,
                tracking_number=tracking_number,
                dispatch_details_status=DispatchDetailsStatus.COMPLETE.value,
                closed_at=delivered_at + timedelta(hours=2),
                promised_eta=promised_eta,
                revised_eta=None,
                follow_up_count=follow_ups,
                pre_eta_checked=(part.criticality == Criticality.CRITICAL.value),
            )
            session.add(po)
            session.flush()

            session.add(
                GoodsReceipt(
                    po_id=po.id,
                    received_qty=order_qty,
                    received_at=delivered_at,
                    condition="GOOD",
                    status=GRNStatus.RELEASED.value,
                )
            )
            session.add(
                InventoryTransaction(
                    part_id=part.id,
                    type=InventoryTxType.RECEIPT_RELEASED.value,
                    qty_delta=order_qty,
                    timestamp=delivered_at,
                    note=f"Historical receipt for {po_num} (seeded)",
                )
            )
            tx_count += 1

            if had_invoice_mismatch:
                bad_price = round(spec.primary_price * 1.15, 2)
                session.add(
                    Invoice(
                        po_id=po.id,
                        invoice_number=f"INV-{po_counter}-A",
                        pdf_path="",
                        extracted_qty=order_qty,
                        extracted_unit_price=bad_price,
                        extracted_total=round(order_qty * bad_price, 2),
                        match_status=InvoiceMatchStatus.MISMATCH_PRICE.value,
                        discrepancy_summary=(
                            f"Historical price overbill ₹{bad_price:.2f} "
                            f"vs ₹{spec.primary_price:.2f}"
                        ),
                        version=1,
                        received_at=delivered_at + timedelta(hours=3),
                    )
                )
                session.add(
                    Invoice(
                        po_id=po.id,
                        invoice_number=f"INV-{po_counter}-B",
                        pdf_path="",
                        extracted_qty=order_qty,
                        extracted_unit_price=spec.primary_price,
                        extracted_total=round(order_qty * spec.primary_price, 2),
                        match_status=InvoiceMatchStatus.MATCHED.value,
                        discrepancy_summary="Corrected invoice passed 3-way match.",
                        version=2,
                        received_at=delivered_at + timedelta(days=2),
                    )
                )
            else:
                session.add(
                    Invoice(
                        po_id=po.id,
                        invoice_number=f"INV-{po_counter}",
                        pdf_path="",
                        extracted_qty=order_qty,
                        extracted_unit_price=spec.primary_price,
                        extracted_total=round(order_qty * spec.primary_price, 2),
                        match_status=InvoiceMatchStatus.MATCHED.value,
                        discrepancy_summary="3-way match passed.",
                        version=1,
                        received_at=delivered_at + timedelta(hours=2),
                    )
                )
            closed_po_count += 1

    # 4. Seed 4 live in-flight POs across distinct non-hero parts for immediate demo readiness.
    # BRG-402 stays clean (on_order=0) for immediate Auto-PO & Making-request demos.
    flt_108 = parts_by_sku["FLT-108"]
    rly_901 = parts_by_sku["RLY-901"]
    blt_301 = parts_by_sku["BLT-301"]
    shf_605 = parts_by_sku["SHF-605"]

    po_1037 = PurchaseOrder(
        po_number=f"PO-{po_counter}",
        part_id=flt_108.id,
        supplier_id=suppliers_by_key[PersonaKey.SUP1.value].id,
        qty=10,
        unit_price=320.0,
        status=POStatus.ACKNOWLEDGED.value,
        notes="Acknowledged by Apex Precision; BlueDart tracking provided; ETA in 4 days.",
        created_at=sim_now - timedelta(days=6, hours=3),
        sent_at=sim_now - timedelta(days=6),
        acknowledged_at=sim_now - timedelta(days=5, hours=18),
        promised_dispatch_date=sim_now + timedelta(days=1),
        carrier_name="BlueDart",
        tracking_number="BD-440192",
        dispatch_details_status=DispatchDetailsStatus.COMPLETE.value,
        promised_eta=sim_now + timedelta(days=4),
        follow_up_count=0,
        pre_eta_checked=False,
    )
    po_1038 = PurchaseOrder(
        po_number=f"PO-{po_counter + 1}",
        part_id=rly_901.id,
        supplier_id=suppliers_by_key[PersonaKey.SUP3.value].id,
        qty=10,
        unit_price=100.0,
        status=POStatus.SENT_AWAITING_ACK.value,
        notes="Dispatched to Deccan Electro-Spares 24h ago; awaiting acknowledgement.",
        created_at=sim_now - timedelta(days=1, hours=2),
        sent_at=sim_now - timedelta(days=1),
        promised_eta=sim_now + timedelta(days=13),
        follow_up_count=0,
        pre_eta_checked=False,
    )
    po_1039 = PurchaseOrder(
        po_number=f"PO-{po_counter + 2}",
        part_id=blt_301.id,
        supplier_id=suppliers_by_key[PersonaKey.SUP2.value].id,
        qty=50,
        unit_price=100.0,
        status=POStatus.ACKNOWLEDGED.value,
        notes="Acknowledged by Bharat Industrial Fasteners; carrier & tracking not yet shared.",
        created_at=sim_now - timedelta(days=5, hours=2),
        sent_at=sim_now - timedelta(days=5),
        acknowledged_at=sim_now - timedelta(days=4, hours=12),
        dispatch_details_status=DispatchDetailsStatus.NONE.value,
        promised_eta=sim_now + timedelta(days=10),
        follow_up_count=0,
        pre_eta_checked=False,
    )
    po_1040 = PurchaseOrder(
        po_number=f"PO-{po_counter + 3}",
        part_id=shf_605.id,
        supplier_id=suppliers_by_key[PersonaKey.SUP2.value].id,
        qty=10,
        unit_price=800.0,
        status=POStatus.RECEIVED_PENDING_INVOICE.value,
        notes="Goods received at dock (GRN=10, GOOD); awaiting PDF invoice from Bharat Fasteners.",
        created_at=sim_now - timedelta(days=14, hours=2),
        sent_at=sim_now - timedelta(days=14),
        acknowledged_at=sim_now - timedelta(days=13, hours=12),
        promised_dispatch_date=sim_now - timedelta(days=4),
        actual_dispatch_date=sim_now - timedelta(days=3),
        carrier_name="DTDC",
        tracking_number="DT-331040",
        dispatch_details_status=DispatchDetailsStatus.COMPLETE.value,
        promised_eta=sim_now - timedelta(days=1),
        follow_up_count=0,
        pre_eta_checked=True,
    )
    in_flight_pos = [po_1037, po_1038, po_1039, po_1040]
    session.add_all(in_flight_pos)
    session.flush()

    session.add(
        GoodsReceipt(
            po_id=po_1040.id,
            received_qty=10,
            received_at=sim_now - timedelta(hours=2),
            condition="GOOD",
            status=GRNStatus.HELD_QUARANTINE.value,
        )
    )

    # 5. Seed initial AutonomyPolicy rows for Lex (all locked to APPROVAL_REQUIRED in MVP)
    for category in EmailCategory:
        session.add(
            AutonomyPolicy(
                agent_name="lex",
                action_category=category.value,
                mode=AutonomyMode.APPROVAL_REQUIRED.value,
                approved_as_is_count=0,
                approved_edited_count=0,
                rejected_count=0,
                updated_at=sim_now,
            )
        )

    session.commit()
    return {
        "suppliers": len(suppliers_by_key),
        "parts": len(parts_by_sku),
        "transactions": tx_count,
        "closed_pos": closed_po_count,
        "open_pos": len(in_flight_pos),
    }

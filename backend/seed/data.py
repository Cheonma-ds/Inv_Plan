import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.core import clock
from backend.core.db import Base
from backend.core.enums import (
    AutonomyMode,
    CommDirection,
    CommStatus,
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
    POCommunication,
    PurchaseOrder,
    Supplier,
    SupplierMemoryNote,
)
from backend.planning import (
    compute_daily_ewma,
    evaluate_part_recommendation,
    recalculate_all,
)

# Oct 1, 2026 13:00:00 IST == 2026-10-01 07:30:00 UTC
SEED_START_TIME = datetime(2026, 10, 1, 7, 30, 0, tzinfo=timezone.utc)
HISTORY_START_IST_DATE = date(2026, 7, 3)  # 90 completed days: Jul 3 .. Sep 30
HISTORY_DAYS = 90


def _ist_dt(d: date, hour: int = 13, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=clock.IST).astimezone(
        timezone.utc
    )


@dataclass(frozen=True)
class PartSpec:
    sku: str
    name: str
    criticality: Criticality
    target_coverage_days: int
    primary_key: str
    primary_price: float
    primary_quoted_lt: int
    primary_moq: int
    backup_key: str
    backup_price: float
    backup_quoted_lt: int
    backup_moq: int
    profile_kind: str
    cycle_starts: tuple[int, ...]
    target_runway_days: float


PART_SPECS: tuple[PartSpec, ...] = (
    # CRITICAL (5 parts)
    PartSpec(
        sku="BRG-402",
        name="Deep Groove Ball Bearing 6208",
        criticality=Criticality.CRITICAL,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=450.0,
        primary_quoted_lt=10,
        primary_moq=2,
        backup_key=PersonaKey.SUP2.value,
        backup_price=480.0,
        backup_quoted_lt=15,
        backup_moq=2,
        profile_kind="steady_late_ramp",
        cycle_starts=(2, 23, 45, 68),
        target_runway_days=19.0,
    ),
    PartSpec(
        sku="VLV-204",
        name="Hydraulic Servo Solenoid Valve HV-40",
        criticality=Criticality.CRITICAL,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=1500.0,
        primary_quoted_lt=15,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=1620.0,
        backup_quoted_lt=12,
        backup_moq=2,
        profile_kind="mid_aug_spike",
        cycle_starts=(1, 29, 59),
        target_runway_days=16.0,
    ),
    PartSpec(
        sku="SNS-802",
        name="Proximity Inductive Sensor M18",
        criticality=Criticality.CRITICAL,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=600.0,
        primary_quoted_lt=14,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=640.0,
        backup_quoted_lt=10,
        backup_moq=2,
        profile_kind="weekday_heavy",
        cycle_starts=(2, 31, 63),
        target_runway_days=19.0,
    ),
    PartSpec(
        sku="PMP-701",
        name="High-Pressure Coolant Pump",
        criticality=Criticality.CRITICAL,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=2000.0,
        primary_quoted_lt=16,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=2150.0,
        backup_quoted_lt=12,
        backup_moq=2,
        profile_kind="bimodal_bursts",
        cycle_starts=(1, 30, 61),
        target_runway_days=15.0,
    ),
    PartSpec(
        sku="BRG-405",
        name="Taper Roller Bearing 30208",
        criticality=Criticality.CRITICAL,
        target_coverage_days=45,
        primary_key=PersonaKey.SUP3.value,
        primary_price=1850.0,
        primary_quoted_lt=18,
        primary_moq=2,
        backup_key=PersonaKey.SUP1.value,
        backup_price=1950.0,
        backup_quoted_lt=14,
        backup_moq=2,
        profile_kind="intermittent_20d",
        cycle_starts=(3, 48),
        target_runway_days=35.0,
    ),
    # STANDARD (6 parts)
    PartSpec(
        sku="FLT-108",
        name="Hydraulic Return Line Filter",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=320.0,
        primary_quoted_lt=10,
        primary_moq=5,
        backup_key=PersonaKey.SUP2.value,
        backup_price=305.0,
        backup_quoted_lt=15,
        backup_moq=5,
        profile_kind="alternating_weeks",
        cycle_starts=(3, 24, 46, 69),
        target_runway_days=18.5,
    ),
    PartSpec(
        sku="BLT-301",
        name="M12 Alloy Hex Bolt Pack (100pc)",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=100.0,
        primary_quoted_lt=15,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=108.0,
        backup_quoted_lt=10,
        backup_moq=10,
        profile_kind="seasonal_climb",
        cycle_starts=(2, 30, 60),
        target_runway_days=31.0,
    ),
    PartSpec(
        sku="GSK-512",
        name="Viton High-Temp Gasket",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=200.0,
        primary_quoted_lt=15,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=215.0,
        backup_quoted_lt=10,
        backup_moq=10,
        profile_kind="smooth_rising",
        cycle_starts=(3, 32, 61),
        target_runway_days=32.0,
    ),
    PartSpec(
        sku="SHF-605",
        name="Precision Linear Shaft 25mm",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=800.0,
        primary_quoted_lt=15,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=860.0,
        backup_quoted_lt=10,
        backup_moq=5,
        profile_kind="cooling_downward",
        cycle_starts=(2, 31, 60),
        target_runway_days=33.0,
    ),
    PartSpec(
        sku="RLY-901",
        name="PLC Safety Relay Module 24V",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=100.0,
        primary_quoted_lt=14,
        primary_moq=5,
        backup_key=PersonaKey.SUP1.value,
        backup_price=108.0,
        backup_quoted_lt=10,
        backup_moq=5,
        profile_kind="late_sep_surge",
        cycle_starts=(3, 33, 64),
        target_runway_days=24.0,
    ),
    PartSpec(
        sku="BLT-305",
        name="Timing Belt HTD-8M-1200",
        criticality=Criticality.STANDARD,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=640.0,
        primary_quoted_lt=10,
        primary_moq=5,
        backup_key=PersonaKey.SUP3.value,
        backup_price=620.0,
        backup_quoted_lt=14,
        backup_moq=5,
        profile_kind="mid_aug_dip",
        cycle_starts=(2, 24, 47, 70),
        target_runway_days=20.0,
    ),
    # LOW (4 parts)
    PartSpec(
        sku="WSH-104",
        name="Stainless Flat Washer M12",
        criticality=Criticality.LOW,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP1.value,
        primary_price=45.0,
        primary_quoted_lt=8,
        primary_moq=25,
        backup_key=PersonaKey.SUP3.value,
        backup_price=42.0,
        backup_quoted_lt=12,
        backup_moq=25,
        profile_kind="high_vel_smooth",
        cycle_starts=(2, 19, 37, 56, 74),
        target_runway_days=22.0,
    ),
    PartSpec(
        sku="ORG-112",
        name="Silicone O-Ring Assortment Box",
        criticality=Criticality.LOW,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP2.value,
        primary_price=240.0,
        primary_quoted_lt=15,
        primary_moq=10,
        backup_key=PersonaKey.SUP1.value,
        backup_price=260.0,
        backup_quoted_lt=10,
        backup_moq=10,
        profile_kind="low_vol_steady",
        cycle_starts=(10, 52),  # 2 cycles (< 3 closed deliveries for LT cold-start test)
        target_runway_days=25.0,
    ),
    PartSpec(
        sku="CBL-114",
        name="Brass Cable Gland PG13.5",
        criticality=Criticality.LOW,
        target_coverage_days=30,
        primary_key=PersonaKey.SUP3.value,
        primary_price=85.0,
        primary_quoted_lt=10,
        primary_moq=25,
        backup_key=PersonaKey.SUP1.value,
        backup_price=92.0,
        backup_quoted_lt=8,
        backup_moq=25,
        profile_kind="high_vel_ramp",
        cycle_starts=(2, 23, 45, 68),
        target_runway_days=20.0,
    ),
    PartSpec(
        sku="FUS-115",
        name="Panel Mount Fuse Holder 10A",
        criticality=Criticality.LOW,
        target_coverage_days=45,
        primary_key=PersonaKey.SUP1.value,
        primary_price=190.0,
        primary_quoted_lt=9,
        primary_moq=10,
        backup_key=PersonaKey.SUP2.value,
        backup_price=180.0,
        backup_quoted_lt=15,
        backup_moq=10,
        profile_kind="intermittent_sporadic",
        cycle_starts=(6, 54),
        target_runway_days=28.0,
    ),
)


def _generate_90d_profile(kind: str, rng: random.Random) -> list[int]:
    """Generate a deterministic 90-day daily consumption sequence (Jul 3 .. Sep 30)."""
    seq: list[int] = []
    for i in range(HISTORY_DAYS):
        d = HISTORY_START_IST_DATE + timedelta(days=i)
        is_weekend = d.weekday() >= 5

        if kind == "steady_late_ramp":
            base = 5.0 if i < 65 else 5.0 + (i - 64) * 0.14
            val = int(round(base + rng.choice((-1, 0, 0, 1))))
        elif kind == "mid_aug_spike":
            spike = 4.0 if 35 <= i <= 48 else 0.0
            val = int(round(3.8 + spike + rng.choice((-1, 0, 0, 1))))
        elif kind == "weekday_heavy":
            val = rng.choice((0, 0, 1)) if is_weekend else rng.randint(4, 6)
        elif kind == "bimodal_bursts":
            burst = (i % 15) in (3, 4, 5)
            val = rng.randint(5, 7) if burst else rng.randint(2, 3)
        elif kind == "intermittent_20d":
            val = rng.randint(4, 6) if i in (8, 25, 42, 60, 79, 87) else 0
        elif kind == "alternating_weeks":
            high_week = (i // 7) % 2 == 0
            val = rng.randint(5, 7) if high_week else rng.randint(2, 3)
        elif kind == "seasonal_climb":
            base = 5.8 + (i / 89.0) * 3.0
            val = int(round(base + rng.choice((-1, 0, 1))))
        elif kind == "smooth_rising":
            base = 4.8 + (i / 89.0) * 2.2
            val = int(round(base + rng.choice((-1, 0, 0, 1))))
        elif kind == "cooling_downward":
            base = 5.2 - (i / 89.0) * 2.7
            val = max(1, int(round(base + rng.choice((-1, 0, 0, 1)))))
        elif kind == "late_sep_surge":
            base = 3.0 if i < 72 else 3.0 + (i - 71) * 0.18
            val = int(round(base + rng.choice((0, 0, 1))))
        elif kind == "mid_aug_dip":
            base = 2.0 if 38 <= i <= 50 else 4.2
            val = max(1, int(round(base + rng.choice((-1, 0, 0, 1)))))
        elif kind == "high_vel_smooth":
            val = rng.randint(9, 11)
        elif kind == "low_vol_steady":
            val = rng.choice((1, 2, 2, 3))
        elif kind == "high_vel_ramp":
            base = 7.8 + (i / 89.0) * 3.2
            val = int(round(base + rng.choice((-1, 0, 1))))
        elif kind == "intermittent_sporadic":
            val = rng.randint(4, 8) if i in (12, 29, 46, 65, 81, 88) else 0
        else:
            val = 3

        seq.append(max(0, val))
    return seq


def _build_supplier_cycle_behavior(
    sup_key: str,
    cycle_global_idx: int,
    rng: random.Random,
) -> tuple[int, int, float, bool]:
    """Return (lt_delta_days, follow_up_count, ack_delay_hours, had_invoice_mismatch)."""
    if sup_key == PersonaKey.SUP1.value:
        lt_delta = 0 if (cycle_global_idx % 12) != 11 else 1
        follow_ups = 0 if (cycle_global_idx % 14) != 13 else 1
        ack_hours = round(rng.uniform(5.0, 9.0), 2)
        mismatch = False
    elif sup_key == PersonaKey.SUP2.value:
        lt_delta = 0 if (cycle_global_idx % 6) == 2 else rng.randint(6, 10)
        follow_ups = 0 if (cycle_global_idx % 5) != 4 else 1
        ack_hours = round(rng.uniform(21.0, 32.0), 2)
        mismatch = (cycle_global_idx % 3) == 1
    else:
        lt_delta = 0 if (cycle_global_idx % 4) != 3 else rng.randint(3, 5)
        follow_ups = (
            0
            if (cycle_global_idx % 5) in (0, 2)
            else (1 if (cycle_global_idx % 3) != 2 else 2)
        )
        ack_hours = round(rng.uniform(42.0, 68.0), 2)
        mismatch = (cycle_global_idx % 7) == 5
    return lt_delta, follow_ups, ack_hours, mismatch


def seed_database(session: Session, *, reset: bool = False) -> dict[str, int]:
    """Populate a fresh DB with 90-day Jul-Sep causal history and 0 active POs on Oct 1."""
    if reset:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.flush()
        session.expunge_all()

    rng = random.Random(42)
    sim_now = clock.set_time(session, SEED_START_TIME)

    # 1. Seed 3 suppliers (scores & avg_response_days & cycles_90d recomputed via planning/)
    suppliers_by_key: dict[str, Supplier] = {}
    supplier_defs = [
        (
            PersonaKey.SUP1.value,
            "Apex Precision Components",
            "orders@apexprecision.in",
            "+91-80-4112-8801",
            "Reliable primary partner; consistently honors quoted lead times and accurate billing.",
        ),
        (
            PersonaKey.SUP2.value,
            "Bharat Industrial Fasteners",
            "sales@bharatfasteners.in",
            "+91-20-6624-3190",
            "Chronically delivers 7-10 days past quoted 15d lead time; verify unit prices on PDFs.",
        ),
        (
            PersonaKey.SUP3.value,
            "Deccan Electro-Spares",
            "dispatch@deccanelectro.in",
            "+91-40-2378-4412",
            (
                "Slow to acknowledge POs (frequently misses 48h SLA) "
                "and prone to mid-quarter price hikes."
            ),
        ),
    ]

    for persona_key, name, email, phone, mem_note in supplier_defs:
        sup = Supplier(
            persona_key=persona_key,
            name=name,
            email=email,
            phone=phone,
        )
        session.add(sup)
        session.flush()
        suppliers_by_key[persona_key] = sup

        session.add(
            SupplierMemoryNote(
                supplier_id=sup.id,
                note=mem_note,
                source=SupplierMemorySource.MANAGER_ADDED.value,
                created_at=sim_now - timedelta(days=60),
                updated_at=sim_now - timedelta(days=60),
            )
        )

    # 2. Seed 15 parts + Primary & Backup supplier terms + causal Jul-Sep simulation
    parts_by_sku: dict[str, Part] = {}
    tx_count = 0
    po_counter = 1001
    closed_po_count = 0
    sup_cycle_counter: dict[str, int] = {
        PersonaKey.SUP1.value: 0,
        PersonaKey.SUP2.value: 0,
        PersonaKey.SUP3.value: 0,
    }

    for spec in PART_SPECS:
        part = Part(
            sku=spec.sku,
            name=spec.name,
            criticality=spec.criticality.value,
            on_hand_stock=0,
            quarantined_stock=0,
            rop=10,
            replenishment_point=30,
            target_coverage_days=spec.target_coverage_days,
            manual_demand_override=None,
            burn_carry=0.0,
            last_burn_settled_date=None,
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
                    effective_lead_time_days=float(spec.primary_quoted_lt),
                    lead_time_std_dev=0.0,
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
                    effective_lead_time_days=float(spec.backup_quoted_lt),
                    lead_time_std_dev=0.0,
                    moq=spec.backup_moq,
                    last_validated_at=validated_at,
                ),
            ]
        )
        session.flush()

        demands = _generate_90d_profile(spec.profile_kind, rng)

        # Schedule historical PO cycles inside days 0..88 (Jul 3 .. Sep 29)
        cycles: list[dict] = []
        for placed_idx in spec.cycle_starts:
            c_idx = sup_cycle_counter[spec.primary_key]
            sup_cycle_counter[spec.primary_key] = c_idx + 1
            lt_delta, follow_ups, ack_hours, mismatch = _build_supplier_cycle_behavior(
                spec.primary_key, c_idx, rng
            )
            # Guarantee every seeded PO closes on or before Sep 29 (day_idx <= 88)
            raw_deliv = placed_idx + spec.primary_quoted_lt + lt_delta
            deliv_idx = min(87 if mismatch else 88, raw_deliv)
            cycles.append(
                {
                    "placed_idx": placed_idx,
                    "deliv_idx": deliv_idx,
                    "lt_delta": deliv_idx - (placed_idx + spec.primary_quoted_lt),
                    "follow_ups": follow_ups,
                    "ack_hours": ack_hours,
                    "mismatch": mismatch,
                }
            )

        # Calibrate initial Jul 3 stock & per-cycle order quantities so stock trajectories
        # show realistic cycles (including occasional low-stock/stockout on late Sup2 cycles)
        # and land at target_runway_days on Oct 1.
        late_avg = max(0.5, sum(demands[-14:]) / 14.0)
        target_oct1_stock = max(
            spec.primary_moq, int(round(spec.target_runway_days * late_avg))
        )
        if spec.profile_kind in ("intermittent_20d", "intermittent_sporadic"):
            target_oct1_stock = max(
                spec.primary_moq * 2, int(round(spec.target_runway_days * 0.45))
            )

        first_deliv = cycles[0]["deliv_idx"]
        # Leave a brief realistic dip/stockout before cycle 1 arrival on Sup2 late items
        pre_first_demand = sum(demands[:first_deliv])
        if spec.primary_key == PersonaKey.SUP2.value and cycles[0]["lt_delta"] >= 5:
            initial_stock = max(
                spec.primary_moq, pre_first_demand - max(1, demands[first_deliv - 1])
            )
        else:
            initial_stock = pre_first_demand + max(2, int(round(late_avg * 2.5)))

        # Compute cycle order quantities to cover each cycle span + target Oct 1 runway
        for idx, cyc in enumerate(cycles):
            d_start = cyc["deliv_idx"]
            if idx + 1 < len(cycles):
                d_end = cycles[idx + 1]["deliv_idx"]
                span_demand = sum(demands[d_start:d_end])
                # On late Sup2 middle cycles, tighten slightly for natural low-stock tension
                is_late_sup2 = (
                    spec.primary_key == PersonaKey.SUP2.value and cyc["lt_delta"] >= 6
                )
                buffer_adj = -1 if is_late_sup2 else 2
                raw_qty = max(spec.primary_moq, span_demand + buffer_adj)
            else:
                remaining_demand = sum(demands[d_start:HISTORY_DAYS])
                raw_qty = max(spec.primary_moq, remaining_demand + target_oct1_stock)
            cyc["qty"] = int(raw_qty)

        # Simulate forward day 0..89 logging exact InventoryTransactions and causal POs
        deliveries_by_day: dict[int, list[dict]] = {}
        for cyc in cycles:
            deliveries_by_day.setdefault(cyc["deliv_idx"], []).append(cyc)

        sim_on_hand = initial_stock
        for day_idx in range(HISTORY_DAYS):
            cur_date = HISTORY_START_IST_DATE + timedelta(days=day_idx)

            # Process any PO deliveries arriving on cur_date
            for cyc in deliveries_by_day.get(day_idx, []):
                placed_date = HISTORY_START_IST_DATE + timedelta(days=cyc["placed_idx"])
                created_at = _ist_dt(placed_date, 9, 0)
                sent_at = created_at + timedelta(hours=1)
                quoted_lt = spec.primary_quoted_lt
                promised_eta = sent_at + timedelta(days=quoted_lt)
                acknowledged_at = sent_at + timedelta(hours=cyc["ack_hours"])
                delivered_at = _ist_dt(cur_date, 10, 0)
                promised_dispatch = promised_eta - timedelta(days=3)
                actual_dispatch = delivered_at - timedelta(days=3)
                carrier_options = ("BlueDart", "Delhivery", "DTDC")
                carrier_name = carrier_options[po_counter % len(carrier_options)]
                carrier_prefix = {"BlueDart": "BD", "Delhivery": "DLV", "DTDC": "DT"}[
                    carrier_name
                ]
                order_qty = cyc["qty"]
                po_num = f"PO-{po_counter}"
                tracking_number = f"{carrier_prefix}-{po_counter * 97}"
                po_counter += 1

                closed_at = (
                    min(_ist_dt(date(2026, 9, 29), 18, 0), delivered_at + timedelta(days=1))
                    if cyc["mismatch"]
                    else delivered_at + timedelta(hours=3)
                )

                po = PurchaseOrder(
                    po_number=po_num,
                    part_id=part.id,
                    supplier_id=primary_sup.id,
                    qty=order_qty,
                    unit_price=spec.primary_price,
                    status=POStatus.CLOSED.value,
                    notes="Historical 90d completed PO (seeded).",
                    created_at=created_at,
                    sent_at=sent_at,
                    acknowledged_at=acknowledged_at,
                    promised_dispatch_date=promised_dispatch,
                    actual_dispatch_date=actual_dispatch,
                    carrier_name=carrier_name,
                    tracking_number=tracking_number,
                    dispatch_details_status=DispatchDetailsStatus.COMPLETE.value,
                    closed_at=closed_at,
                    promised_eta=promised_eta,
                    revised_eta=None,
                    follow_up_count=cyc["follow_ups"],
                    pre_eta_checked=(part.criticality == Criticality.CRITICAL.value),
                )
                session.add(po)
                session.flush()

                # Persist realistic POCommunication trail
                thread_id = f"seed-thread-{po_num}"
                session.add(
                    POCommunication(
                        po_id=po.id,
                        thread_id=thread_id,
                        direction=CommDirection.OUTBOUND.value,
                        status=CommStatus.SENT.value,
                        category=EmailCategory.PO_DISPATCH.value,
                        from_persona=PersonaKey.LEX.value,
                        to_personas=[primary_sup.persona_key],
                        cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                        subject=f"[{po_num}] Purchase Order for {part.sku}",
                        body=(
                            f"Dear {primary_sup.name}, please confirm {po_num} for "
                            f"{order_qty} units of {part.sku}."
                        ),
                        timestamp=sent_at,
                    )
                )
                for f_idx in range(cyc["follow_ups"]):
                    nudge_ts = min(
                        acknowledged_at - timedelta(hours=1),
                        sent_at + timedelta(hours=24 * (f_idx + 1)),
                    )
                    session.add(
                        POCommunication(
                            po_id=po.id,
                            thread_id=thread_id,
                            direction=CommDirection.OUTBOUND.value,
                            status=CommStatus.SENT.value,
                            category=EmailCategory.ACK_REMINDER.value,
                            from_persona=PersonaKey.LEX.value,
                            to_personas=[primary_sup.persona_key],
                            cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                            subject=f"[{po_num}] Acknowledgement Follow-Up #{f_idx + 1}",
                            body=f"Following up on {po_num} acknowledgement.",
                            timestamp=nudge_ts,
                        )
                    )
                session.add(
                    POCommunication(
                        po_id=po.id,
                        thread_id=thread_id,
                        direction=CommDirection.INBOUND.value,
                        status=CommStatus.RECEIVED.value,
                        category=EmailCategory.GENERAL.value,
                        from_persona=primary_sup.persona_key,
                        to_personas=[PersonaKey.LEX.value],
                        cc_personas=[PersonaKey.INVENTORY_MANAGER.value],
                        subject=f"Re: [{po_num}] Order Confirmed",
                        body=(
                            f"Confirmed {po_num} for {order_qty} units via {carrier_name} "
                            f"({tracking_number})."
                        ),
                        timestamp=acknowledged_at,
                    )
                )

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
                        is_predicted_burn=False,
                        note=f"Historical receipt for {po_num} (seeded)",
                    )
                )
                tx_count += 1
                sim_on_hand += order_qty

                if cyc["mismatch"]:
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
                            received_at=delivered_at + timedelta(hours=2),
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
                            received_at=closed_at - timedelta(minutes=30),
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

            # Process day_idx consumption
            planned = demands[day_idx]
            actual_draw = min(sim_on_hand, planned)
            if actual_draw > 0:
                sim_on_hand -= actual_draw
                session.add(
                    InventoryTransaction(
                        part_id=part.id,
                        type=InventoryTxType.CONSUMPTION.value,
                        qty_delta=-actual_draw,
                        timestamp=_ist_dt(cur_date, 18, 0),
                        is_predicted_burn=False,
                        note="Historical daily consumption (seeded)",
                    )
                )
                tx_count += 1

        part.on_hand_stock = sim_on_hand

    session.flush()

    # 3. Compute ALL supplier scores, effective lead times, daily EWMA, and initial ROP/T
    # directly via planning/ from the persisted DB rows (Zero Mismatch Guarantee).
    recalculate_all(session)
    session.flush()

    for part in parts_by_sku.values():
        daily = compute_daily_ewma(session, part, sim_now=sim_now)
        part.daily_burn_ewma = daily.daily_burn_ewma
        part.daily_burn_prev_7d = daily.daily_burn_prev_7d
        part.daily_demand_std = daily.daily_demand_std
        part.burn_carry = 0.0
        part.last_burn_settled_date = "2026-09-30"
        session.flush()

        rec = evaluate_part_recommendation(session, part.id)
        part.rop = max(1, rec.recommended_rop)
        part.replenishment_point = max(part.rop + 1, rec.recommended_replenishment_point)

        # Keep healthy SKUs above ROP so +1d/+3d advances do not flood alerts
        if part.sku not in ("VLV-204", "SNS-802", "PMP-701"):
            min_healthy_stock = part.rop + max(
                4, int(math.ceil(daily.effective_daily_demand * 5.0))
            )
            if part.on_hand_stock < min_healthy_stock:
                diff = min_healthy_stock - part.on_hand_stock
                # Adjust the final historical PO & receipt qty so ledger conservation holds 100%
                last_po = session.scalar(
                    select_last_po_for_part(part.id)
                )
                if last_po is not None:
                    last_po.qty += diff
                    last_grn = session.scalar(
                        select_first_grn_for_po(last_po.id)
                    )
                    if last_grn is not None:
                        last_grn.received_qty += diff
                    last_receipt_tx = session.scalar(
                        select_last_receipt_tx_for_part(part.id)
                    )
                    if last_receipt_tx is not None:
                        last_receipt_tx.qty_delta += diff
                    for inv in session.scalars(select_invoices_for_po(last_po.id)).all():
                        inv.extracted_qty = last_po.qty
                        inv.extracted_total = round(last_po.qty * inv.extracted_unit_price, 2)
                part.on_hand_stock += diff

    # 4. Seed initial AutonomyPolicy rows for Lex (all locked to APPROVAL_REQUIRED in MVP)
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
        "open_pos": 0,
    }


def select_last_po_for_part(part_id: int):
    from sqlalchemy import select

    return (
        select(PurchaseOrder)
        .where(PurchaseOrder.part_id == part_id)
        .order_by(PurchaseOrder.id.desc())
        .limit(1)
    )


def select_first_grn_for_po(po_id: int):
    from sqlalchemy import select

    return (
        select(GoodsReceipt)
        .where(GoodsReceipt.po_id == po_id)
        .order_by(GoodsReceipt.id.asc())
        .limit(1)
    )


def select_last_receipt_tx_for_part(part_id: int):
    from sqlalchemy import select

    return (
        select(InventoryTransaction)
        .where(
            InventoryTransaction.part_id == part_id,
            InventoryTransaction.type == InventoryTxType.RECEIPT_RELEASED.value,
        )
        .order_by(InventoryTransaction.id.desc())
        .limit(1)
    )


def select_invoices_for_po(po_id: int):
    from sqlalchemy import select

    return select(Invoice).where(Invoice.po_id == po_id)

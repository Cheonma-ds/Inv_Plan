from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.core.models import SystemState

SIM_NOW_KEY = "sim_now"
IST = timezone(timedelta(hours=5, minutes=30))


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist_date(dt: datetime) -> date:
    """Convert a datetime to its IST (Asia/Kolkata, UTC+05:30) calendar date."""
    return ensure_utc(dt).astimezone(IST).date()


def _parse_iso_utc(raw: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(raw))


class SimulatedClock:
    """Persisted simulated clock stored in system_state(key='sim_now')."""

    @staticmethod
    def now(session: Session) -> datetime:
        row = session.get(SystemState, SIM_NOW_KEY)
        if row is None:
            initial = _parse_iso_utc(settings.default_sim_start)
            row = SystemState(key=SIM_NOW_KEY, value=initial.isoformat())
            session.add(row)
            session.flush()
            return initial
        return _parse_iso_utc(row.value)

    @staticmethod
    def set_time(session: Session, target: datetime) -> datetime:
        normalized = ensure_utc(target)
        row = session.get(SystemState, SIM_NOW_KEY)
        if row is None:
            row = SystemState(key=SIM_NOW_KEY, value=normalized.isoformat())
            session.add(row)
        else:
            row.value = normalized.isoformat()
        session.flush()
        return normalized

    @staticmethod
    def advance(session: Session, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta < timedelta(0):
            raise ValueError("SimulatedClock cannot move backwards; use set_time() to reset.")
        current = SimulatedClock.now(session)
        return SimulatedClock.set_time(session, current + delta)


def now(session: Session) -> datetime:
    return SimulatedClock.now(session)


def set_time(session: Session, target: datetime) -> datetime:
    return SimulatedClock.set_time(session, target)


def advance(session: Session, *, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    return SimulatedClock.advance(session, days=days, hours=hours, minutes=minutes)

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


@dataclass
class Session:
    """In-memory WhatsApp user session."""

    phone: str
    flow: str  # "order" | "callback"
    stage: str
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """Return True if session TTL has elapsed."""
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at


class SessionManager:
    """Tracks per-user conversation state with an expiry TTL."""

    def __init__(self, ttl_seconds: int = 24 * 60 * 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get(self, phone: str) -> Optional[Session]:
        """Get an active session for this phone, or None."""
        async with self._lock:
            session = self._sessions.get(phone)
            if not session:
                return None
            if session.is_expired():
                self._sessions.pop(phone, None)
                return None
            return session

    async def set_session(self, session: Session) -> None:
        """Store/update a session."""
        async with self._lock:
            self._sessions[session.phone] = session

    async def start_order_flow(self, phone: str, order_type: Optional[str] = None) -> Session:
        """Start or restart the multi-step order flow."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            session = Session(
                phone=phone,
                flow="order",
                stage="name",
                data={},
                created_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            if order_type:
                session.data["order_type"] = order_type
            self._sessions[phone] = session
            return session

    async def start_callback_flow(self, phone: str) -> Session:
        """Start or restart the callback request flow."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            session = Session(
                phone=phone,
                flow="callback",
                stage="name",
                data={},
                created_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            self._sessions[phone] = session
            return session

    async def advance_stage(self, phone: str, stage: str) -> None:
        """Move session to the next stage."""
        async with self._lock:
            session = self._sessions.get(phone)
            if not session or session.is_expired():
                return
            session.stage = stage

    async def update_data(self, phone: str, **kwargs: Any) -> None:
        """Update session data fields."""
        async with self._lock:
            session = self._sessions.get(phone)
            if not session or session.is_expired():
                return
            session.data.update(kwargs)

    async def clear(self, phone: str) -> None:
        """Remove a user's session to interrupt the flow."""
        async with self._lock:
            self._sessions.pop(phone, None)


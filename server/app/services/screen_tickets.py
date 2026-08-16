"""Short-lived, single-use tickets that let a browser open a screen WebSocket.

The browser WebSocket API cannot set an Authorization header, so the viewer
authenticates normally over REST to obtain a ticket, then presents the ticket in
the WebSocket URL. Because the ticket is single-use and expires in seconds, its
appearing in a URL (and therefore possibly a log) is far less dangerous than the
long-lived access token would be.

A ticket is bound to one endpoint and one user, and carries the authorisation
decision that was already made when it was issued -- redeeming it does not
re-open access, it only proves the handshake belongs to that prior decision.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class Ticket:
    endpoint_id: str
    user_id: str
    username: str
    session_id: str
    expires_at: float


class TicketStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def issue(self, *, endpoint_id: str, user_id: str, username: str,
              session_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._tickets[token] = Ticket(
                endpoint_id=endpoint_id,
                user_id=user_id,
                username=username,
                session_id=session_id,
                expires_at=time.monotonic() + ttl_seconds,
            )
        return token

    def redeem(self, token: str) -> Ticket | None:
        """Consume a ticket. Returns it once, then never again."""
        with self._lock:
            self._prune_locked()
            ticket = self._tickets.pop(token, None)
        if ticket is None or ticket.expires_at < time.monotonic():
            return None
        return ticket

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [token for token, t in self._tickets.items() if t.expires_at < now]
        for token in expired:
            del self._tickets[token]

    def reset(self) -> None:
        with self._lock:
            self._tickets.clear()


tickets = TicketStore()

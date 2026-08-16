"""WSGI entrypoint.

Development:  flask --app wsgi run
Production:   python wsgi.py  (single process -- the screen hub lives in memory)

Two supported deployments, both single-process:

  Linux   nginx terminates TLS and forwards to 127.0.0.1:5000 (the defaults).
  Windows no proxy in front, so this process terminates TLS itself using
          EEM_TLS_CERT / EEM_TLS_KEY on 0.0.0.0:443 (set by install.ps1).

Everything is env-driven so neither deployment needs a code change.
"""

import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402  (import after .env is loaded)

app = create_app()


def _bind_settings() -> tuple[str, int, tuple[str, str] | None]:
    """Read host/port/TLS from the environment, defaulting to loopback plaintext."""
    host = os.environ.get("EEM_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("EEM_BIND_PORT", "5000").strip() or 5000)

    cert = os.environ.get("EEM_TLS_CERT", "").strip()
    key = os.environ.get("EEM_TLS_KEY", "").strip()
    if cert and key:
        # Fail loudly here rather than serving plaintext on a port everyone
        # believes is HTTPS.
        for label, path in (("EEM_TLS_CERT", cert), ("EEM_TLS_KEY", key)):
            if not os.path.isfile(path):
                raise SystemExit(f"{label} points at a file that does not exist: {path}")
        return host, port, (cert, key)
    return host, port, None


if __name__ == "__main__":
    bind_host, bind_port, ssl_context = _bind_settings()

    # threaded=True is required, not optional: live screen viewing holds several
    # WebSockets open at once (the agent's, plus every viewer's), and each is a
    # blocking handler. A single-threaded server would deadlock on the first
    # one -- and flask-sock cannot complete the upgrade without it.
    app.run(host=bind_host, port=bind_port, threaded=True, ssl_context=ssl_context)

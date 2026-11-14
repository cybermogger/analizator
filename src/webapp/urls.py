from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def valid_web_app_url(value: str | None) -> bool:
    """Check that the operator supplied a public HTTPS URL for our fixed app route."""
    if not value or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port_valid = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            return False
        try:
            address = ipaddress.ip_address(host) if host else None
        except ValueError:
            address = None
        host_is_public = bool(
            host and "." in host
            and not host.endswith((".localhost", ".local", ".test", ".example", ".invalid"))
            and host not in {"example.com", "example.net", "example.org"}
            and not host.endswith((".example.com", ".example.net", ".example.org"))
        )
        if address is not None:
            host_is_public = address.is_global
        return bool(
            parsed.scheme == "https" and host_is_public and port_valid
            and parsed.path == "/app/" and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False

"""Minimal Ctrip state with optional expiry. Never log or write plaintext state."""
import base64
import json
import os
import time
from urllib.parse import urlsplit
from core.collection import FetchFailure

SECRET_NAME = "CTRIP_STORAGE_STATE_B64"
MAX_BYTES = 47000


def ctrip_host(host):
    host = (host or "").lower().strip(".")
    return host == "ctrip.com" or host.endswith(".ctrip.com")


def minimal_state(state):
    cookies = [c for c in state.get("cookies", []) if ctrip_host(c.get("domain"))]
    origins = [o for o in state.get("origins", [])
               if urlsplit(o.get("origin", "")).scheme == "https"
               and ctrip_host(urlsplit(o.get("origin", "")).hostname)]
    if not cookies:
        raise ValueError("No Ctrip cookies in this profile; confirm the browser channel used for login")
    return {"cookies": cookies, "origins": origins}


def encode_state(state, now=None, ttl_days=None):
    now = int(time.time() if now is None else now)
    if ttl_days is not None and (type(ttl_days) is not int or ttl_days <= 0):
        raise ValueError("Optional validity must be a positive number of days")
    package = {"version": 2, "issued_at": now,
               "expires_at": now + ttl_days * 86400 if ttl_days is not None else None,
               "state": minimal_state(state)}
    encoded = base64.b64encode(json.dumps(package, separators=(",", ":")).encode()).decode("ascii")
    if len(encoded) > MAX_BYTES:
        raise ValueError("Minimal state exceeds the supported GitHub Secret size")
    return encoded


def decode_state(value, now=None):
    try:
        if len(value) > MAX_BYTES:
            raise ValueError()
        package = json.loads(base64.b64decode(value, validate=True))
        now = int(time.time() if now is None else now)
        if type(package["issued_at"]) is not int or package["issued_at"] > now:
            raise ValueError()
        if package["version"] == 1:
            # v1 only supported a mandatory 7-day cap. The user has revoked that
            # policy; retain existing uploads without changing real cookie expiry.
            if package.get("expires_at") != package["issued_at"] + 7 * 86400:
                raise ValueError()
        elif package["version"] == 2:
            expires = package["expires_at"]
            if expires is not None and (type(expires) is not int or
                    expires <= package["issued_at"] or now >= expires):
                raise ValueError()
        else:
            raise ValueError()
        state = minimal_state(package["state"])
        if state != package["state"]:
            raise ValueError()
        return state
    except Exception:
        raise FetchFailure("login_required", "Cloud login state invalid or expired; renew locally") from None


def consume_cloud_state():
    value = os.environ.pop(SECRET_NAME, None)
    return decode_state(value) if value else None

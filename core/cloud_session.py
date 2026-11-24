"""Per-platform browser state with optional expiry; no plaintext state files."""
import base64
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from core.collection import FetchFailure

SECRET_NAME = "CTRIP_STORAGE_STATE_B64"
SESSION_SECRETS = {"ctrip": SECRET_NAME, "tongcheng": "TONGCHENG_STORAGE_STATE_B64",
                   "fliggy": "FLIGGY_STORAGE_STATE_B64"}
SESSION_DOMAINS = {"ctrip": ("ctrip.com",), "tongcheng": ("ly.com",),
                   "fliggy": ("fliggy.com", "taobao.com")}
MAX_BYTES = 47000


def minimal_state(state, *, platform="ctrip"):
    domains = SESSION_DOMAINS[platform]

    def included(host):
        host = (host or "").lower().strip(".")
        return any(host == domain or host.endswith("." + domain) for domain in domains)

    cookies = [c for c in state.get("cookies", []) if included(c.get("domain"))]
    origins = [o for o in state.get("origins", [])
               if urlsplit(o.get("origin", "")).scheme == "https"
               and included(urlsplit(o.get("origin", "")).hostname)]
    if not cookies:
        raise ValueError(f"No {platform} cookies in this profile; confirm the browser channel used for login")
    return {"cookies": cookies, "origins": origins}


def encode_state(state, now=None, ttl_days=None, *, platform="ctrip"):
    now = int(time.time() if now is None else now)
    if ttl_days is not None and (type(ttl_days) is not int or ttl_days <= 0):
        raise ValueError("Optional validity must be a positive number of days")
    package = {"version": 2, "issued_at": now,
               "expires_at": now + ttl_days * 86400 if ttl_days is not None else None,
               "state": minimal_state(state, platform=platform)}
    if platform != "ctrip":
        package["platform"] = platform
    encoded = base64.b64encode(json.dumps(package, separators=(",", ":")).encode()).decode("ascii")
    if len(encoded) > MAX_BYTES:
        raise ValueError("Minimal state exceeds the supported GitHub Secret size")
    return encoded


def decode_state(value, now=None, *, platform="ctrip"):
    try:
        if len(value) > MAX_BYTES:
            raise ValueError()
        package = json.loads(base64.b64decode(value, validate=True))
        if package.get("platform", "ctrip") != platform:
            raise ValueError()
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
        state = minimal_state(package["state"], platform=platform)
        if state != package["state"]:
            raise ValueError()
        return state
    except Exception:
        raise FetchFailure("login_required", "Cloud login state invalid or expired; renew locally") from None


def consume_cloud_state():
    value = os.environ.pop(SECRET_NAME, None)
    return decode_state(value) if value else None


def detach_cloud_states():
    """Remove all credentials before starting any browser or driver process."""
    return {platform: os.environ.pop(secret, "") for platform, secret in SESSION_SECRETS.items()}


@contextmanager
def cloud_desktop(state, viewport):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            with browser.new_context(storage_state=state, viewport=viewport,
                                     locale="zh-CN", timezone_id="Asia/Shanghai") as ctx:
                yield ctx
        finally:
            browser.close()

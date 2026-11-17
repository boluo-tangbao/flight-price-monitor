"""Upload Ctrip state directly to an encrypted, main-only environment Secret.

No password prompts, raw-state files, clipboard or secret stdout. Uses Git's
existing GitHub credential helper for normal GitHub API authentication.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import base64
import json
import os
import re
import subprocess
import urllib.request
import urllib.error
from core.cloud_session import encode_state, SECRET_NAME


class UploadError(Exception):
    pass


def github_token(project):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    result = subprocess.run(["git", "credential", "fill"], cwd=project, env=env,
                            input="protocol=https\nhost=github.com\n\n", capture_output=True,
                            text=True, timeout=30)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not fields.get("password"):
        raise UploadError("GitHub authentication unavailable; sign in to Git Credential Manager first")
    return fields["password"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(token, method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request("https://api.github.com" + path, data=body, method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "flight-price-monitor-session-setup"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == "GET":
            return None
        raise UploadError(f"GitHub API HTTP {error.code}; Secret was not confirmed uploaded") from None
    except urllib.error.URLError:
        raise UploadError("GitHub API connection failed; Secret was not confirmed uploaded") from None


def prepare_environment(token, repo, environment):
    root = f"/repos/{repo}/environments/{environment}"
    current = api(token, "GET", root)
    if current is None:
        api(token, "PUT", root, {"deployment_branch_policy":
                                {"protected_branches": False, "custom_branch_policies": True}})
        api(token, "POST", root + "/deployment-branch-policies", {"name": "main", "type": "branch"})
    else:
        policy = current.get("deployment_branch_policy") or {}
        rules = api(token, "GET", root + "/deployment-branch-policies") or {}
        entries = rules.get("branch_policies", [])
        if (not policy.get("custom_branch_policies") or len(entries) != 1
                or entries[0].get("name") != "main" or entries[0].get("type", "branch") != "branch"):
            raise UploadError("Existing environment is not restricted to main; adjust its branch policy first")
    key = api(token, "GET", root + "/secrets/public-key")
    if not key or not key.get("key"):
        raise UploadError("Unable to read environment public key")
    return root, key


def read_local_state(project, channel):
    from playwright.sync_api import sync_playwright
    profile = project / "user_data" / ("ctrip" if channel == "chromium" else "ctrip-" + channel)
    if not profile.is_dir():
        raise UploadError("Saved login profile not found for the requested channel")
    with sync_playwright() as p:
        options = {"headless": True, "proxy": {"server": "http://127.0.0.1:9"},
                   "args": ["--disable-background-networking", "--disable-component-update", "--disable-sync"]}
        if channel != "chromium": options["channel"] = channel
        context = p.chromium.launch_persistent_context(str(profile), **options)
        try:
            return context.storage_state(indexed_db=True)
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description="Securely upload existing Ctrip login state; no site navigation")
    parser.add_argument("--repo", default="boluo-tangbao/flight-price-monitor")
    parser.add_argument("--environment", default="ctrip-cloud")
    parser.add_argument("--browser-channel", choices=["msedge", "chrome", "chromium"], default="msedge")
    args = parser.parse_args()
    if args.repo != "boluo-tangbao/flight-price-monitor" or not re.fullmatch(r"[A-Za-z0-9_-]+", args.environment):
        parser.error("Only the intended repository and a simple environment name are allowed")
    try:
        from nacl.public import PublicKey, SealedBox
        from nacl.encoding import Base64Encoder
        project = Path(__file__).resolve().parents[1]
        token = github_token(project)
        root, key = prepare_environment(token, args.repo, args.environment)
        state = read_local_state(project, args.browser_channel)
        encoded = encode_state(state)
        encrypted = SealedBox(PublicKey(key["key"], Base64Encoder)).encrypt(encoded.encode("ascii"))
        api(token, "PUT", root + "/secrets/" + SECRET_NAME,
            {"encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key["key_id"]})
        info = api(token, "GET", root + "/secrets/" + SECRET_NAME)
        if not info or info.get("name") != SECRET_NAME:
            raise UploadError("Upload response could not be confirmed")
        print(f"Uploaded {SECRET_NAME} to {args.repo} / {args.environment}; main branch only.")
        print("Only Ctrip state was included; no plaintext state file was written. Renew within 7 days.")
        return 0
    except ImportError:
        print("Install local helper dependency: python -m pip install -r requirements-session.txt", file=sys.stderr)
    except (UploadError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
    except Exception:
        print("Session upload failed; close the project's browser and check environment permissions. No secret values printed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

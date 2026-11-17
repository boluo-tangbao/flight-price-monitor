import base64
import io
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from nacl.public import PrivateKey, SealedBox
from nacl.encoding import Base64Encoder
from scripts import upload_ctrip_session as upload
from core.cloud_session import SECRET_NAME, decode_state


class SessionUploadTests(unittest.TestCase):
    def test_api_uses_direct_route_without_inheriting_system_proxy(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            upload.api("synthetic-token", "GET", "/repos/owner/repo")
        self.assertEqual(opener.call_args.args[0].proxies, {})

    def test_encrypted_upload_contains_only_allowed_state_and_no_secret_output(self):
        key = PrivateKey.generate()
        encrypted = []
        calls = []
        state = {"cookies": [{"domain": ".ctrip.com", "name": "test", "value": "synthetic-cookie"},
                             {"domain": ".other.test", "name": "other", "value": "discard"}], "origins": []}
        def api(token, method, path, data=None):
            calls.append((method, path, data))
            if path.endswith("/secrets/public-key"):
                return {"key": key.public_key.encode(Base64Encoder).decode(), "key_id": "test-key"}
            if path.endswith("/secrets/" + SECRET_NAME):
                if method == "PUT": encrypted.append(data); return {}
                return {"name": SECRET_NAME}
            return None if method == "GET" else {}
        output = io.StringIO()
        with patch("sys.argv", ["upload_ctrip_session.py"]), patch.object(upload, "github_token", return_value="synthetic-token"), \
                patch.object(upload, "api", side_effect=api), patch.object(upload, "read_local_state", return_value=state), \
                patch("sys.stdout", output):
            self.assertEqual(upload.main(), 0)
        plaintext = SealedBox(key).decrypt(base64.b64decode(encrypted[0]["encrypted_value"]))
        decoded = decode_state(plaintext.decode())
        self.assertEqual(len(decoded["cookies"]), 1)
        self.assertNotIn("synthetic-cookie", output.getvalue())
        self.assertNotIn("synthetic-token", output.getvalue())
        rules = [data for method, path, data in calls if path.endswith("deployment-branch-policies")]
        self.assertEqual(rules, [{"name": "main", "type": "branch"}])

    def test_open_environment_refused_before_reading_local_session(self):
        with patch.object(upload, "api", return_value={"deployment_branch_policy": None}):
            with self.assertRaises(upload.UploadError):
                upload.prepare_environment("synthetic", "boluo-tangbao/flight-price-monitor", "ctrip-cloud")

    def test_existing_main_only_environment_accepted(self):
        def api(token, method, path, data=None):
            if path.endswith("public-key"): return {"key": "synthetic"}
            if path.endswith("deployment-branch-policies"):
                return {"branch_policies": [{"name": "main", "type": "branch"}]}
            return {"deployment_branch_policy": {"custom_branch_policies": True}}
        with patch.object(upload, "api", side_effect=api):
            root, key = upload.prepare_environment("synthetic", "boluo-tangbao/flight-price-monitor", "ctrip-cloud")
        self.assertEqual(key["key"], "synthetic")

from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

try:
    from vibenovel.studio.server import BasicAuthStore, _is_loopback_host, _parse_host
except ImportError:
    BasicAuthStore = None
    _is_loopback_host = _parse_host = None


@unittest.skipUnless(BasicAuthStore, "Optional Studio tests: install \".[studio,test]\"")
class StudioRemoteBoundaryUnitTests(unittest.TestCase):
    def test_host_parser_rejects_userinfo_and_bad_ports(self):
        self.assertEqual(_parse_host("192.168.1.20:8000"), ("192.168.1.20", 8000))
        self.assertEqual(_parse_host("[2001:db8::20]:443"), ("2001:db8::20", 443))
        self.assertEqual(_parse_host("CAMPUS.Example."), ("campus.example", None))
        for value in ("user:pass@campus.example", "campus.example:0", "campus.example/path",
                      "campus example", "campus.example:bad"):
            self.assertIsNone(_parse_host(value), value)
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertFalse(_is_loopback_host("192.168.1.20"))

    def test_pbkdf2_credentials_and_file_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "studio.htpasswd"
            salt = b"fixed-test-salt"
            digest = hashlib.pbkdf2_hmac("sha256", b"secret", salt, 100_000)
            encoded = "pbkdf2-sha256$100000$%s$%s" % (
                base64.b64encode(salt).decode(), base64.b64encode(digest).decode())
            path.write_text("author:" + encoded + "\n", encoding="utf-8")
            if os.name != "nt":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            store = BasicAuthStore(path)
            self.assertTrue(store.verify("author", "secret"))
            self.assertFalse(store.verify("author", "wrong"))
            self.assertFalse(store.verify("other", "secret"))


try:
    from fastapi.testclient import TestClient
    from vibenovel.core.project import ProjectStore
    from vibenovel.studio.server import create_app
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "Optional Studio tests: install \".[studio,test]\"")
class StudioCampusBoundaryHTTPTests(unittest.TestCase):
    def test_campus_host_allowlist_and_basic_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ProjectStore(root).init("story", genre="general", title="测试")
            auth = root / "auth"
            salt = b"fixed-test-salt"
            digest = hashlib.pbkdf2_hmac("sha256", b"secret", salt, 100_000)
            auth.write_text("author:pbkdf2-sha256$100000$%s$%s\n" % (
                base64.b64encode(salt).decode(), base64.b64encode(digest).decode()), encoding="utf-8")
            if os.name != "nt":
                auth.chmod(stat.S_IRUSR | stat.S_IWUSR)
            app = create_app(root, frontend_dir=root / "web", access_mode="campus",
                             allowed_hosts=["192.168.1.20"], basic_auth_file=auth)
            with TestClient(app, base_url="http://192.168.1.20:8000") as client:
                self.assertEqual(client.get("/api/bootstrap").status_code, 401)
                basic = base64.b64encode(b"author:secret").decode("ascii")
                headers = {"Authorization": "Basic " + basic}
                self.assertEqual(client.get("/api/bootstrap", headers=headers).status_code, 200)
                self.assertEqual(client.get("/api/bootstrap", headers={**headers, "Host": "192.168.1.21:8000"}).status_code, 403)
                self.assertEqual(client.get("/api/bootstrap", headers={**headers, "Host": "127.0.0.1:8000"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()

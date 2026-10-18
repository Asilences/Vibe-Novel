"""Local API secrets stay local; reload and validation use only temporary files."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from vibenovel.core.documents import DocumentStore
from vibenovel.core.project import ProjectStore
from vibenovel.studio.jobs import JobManager
from vibenovel.studio.providers import OpenAICompatibleProvider, ProviderError, capabilities
from vibenovel.studio.settings import MAX_SETTINGS_BYTES, SettingsError, load_settings


class LocalSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.path = self.workspace / "config" / "api.local.json"
        self.path.parent.mkdir()

    def config(self, **fields):
        api = {"base_url": "https://example.invalid/v1", "model": "local-model", "api_key": "private-local-secret"}
        api.update(fields)
        return {"default_provider": "api", "api": api}

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_missing_or_blank_file_defaults_to_api_without_implicit_destination(self):
        for exists in (False, True):
            if exists:
                self.write(self.config(base_url="", model="", api_key=""))
            settings = load_settings(self.workspace, {})
            self.assertEqual(settings.default_provider, "api")
            self.assertEqual(settings.base_url, "")
            state = capabilities({}, workspace=self.workspace)
            self.assertEqual(state["default_provider"], "api")
            self.assertFalse(state["api"]["available"])
            self.assertNotIn("codex", state)
        self.write(self.config(base_url=""))
        self.assertFalse(capabilities({}, workspace=self.workspace)["api"]["available"])

    def test_file_bom_hot_reload_environment_priority_and_explicit_empty_override(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(self.config()).encode())
        self.assertEqual(load_settings(self.workspace, {}).api_key, "private-local-secret")
        env = {"VIBENOVEL_API_MODEL": "env-model", "VIBENOVEL_API_BASE": "https://env.invalid/v1"}
        settings = load_settings(self.workspace, env)
        self.assertEqual(settings.model, "env-model")
        self.assertEqual(settings.base_url, env["VIBENOVEL_API_BASE"])
        self.assertEqual(settings.api_key, "private-local-secret")
        self.assertEqual(env, {"VIBENOVEL_API_MODEL": "env-model", "VIBENOVEL_API_BASE": "https://env.invalid/v1"})
        self.assertEqual(load_settings(self.workspace, {"VIBENOVEL_API_KEY": ""}).api_key, "")
        self.assertFalse(capabilities({"VIBENOVEL_API_KEY": ""}, workspace=self.workspace)["api"]["available"])
        self.write(self.config(api_key="updated-local-secret", model="updated-model"))
        self.assertEqual(load_settings(self.workspace, {}).api_key, "updated-local-secret")
        self.assertEqual(load_settings(self.workspace, {}).model, "updated-model")
        self.assertNotIn("updated-local-secret", repr(load_settings(self.workspace, {})))

    def test_environment_is_never_mutated_and_complete_env_works_without_file(self):
        env = {"VIBENOVEL_API_BASE": "https://env.invalid/v1", "VIBENOVEL_API_MODEL": "env-model",
               "VIBENOVEL_API_KEY": "env-secret"}
        with patch.dict(os.environ, env, clear=True):
            before = dict(os.environ)
            self.assertTrue(capabilities(workspace=self.workspace)["api"]["available"])
            OpenAICompatibleProvider(workspace=self.workspace)
            self.assertEqual(dict(os.environ), before)

    def test_invalid_schema_fails_closed_and_does_not_echo_field_names_or_values(self):
        bad = [None, [], {}, {"default_provider": "other", "api": {}},
               dict(self.config(), **{"malicious-key-secret": "value"}),
               self.config(**{"malicious-key-secret": "value"}), self.config(api_key=123),
               self.config(model=True), self.config(base_url=None), self.config(allow_loopback=1),
               self.config(allow_loopback="true"), self.config(api_key="secret\nvalue"),
               self.config(model="\ud800")]
        for value in bad:
            with self.subTest(value_type=type(value).__name__):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(SettingsError) as error:
                    load_settings(self.workspace, {})
                state = capabilities({}, workspace=self.workspace)
                self.assertFalse(state["api"]["available"])
                self.assertEqual(state["default_provider"], "api")
                exposed = str(error.exception) + json.dumps(state)
                self.assertNotIn("malicious-key-secret", exposed)
                self.assertNotIn("private-local-secret", exposed)
        self.path.write_text('{"malicious-duplicate-secret":1,"malicious-duplicate-secret":2}', encoding="utf-8")
        with self.assertRaises(SettingsError) as error:
            load_settings(self.workspace, {})
        self.assertNotIn("malicious-duplicate-secret", str(error.exception))

    def test_invalid_encoding_oversize_nested_and_environment_cannot_mask_invalid_file(self):
        for raw in (b"\xff\xfe\x00", b"x" * (MAX_SETTINGS_BYTES + 1), b"[" * 2000 + b"]" * 2000,
                    b'{"bad":NaN}', b'{"api":{},"api":{}}'):
            self.path.write_bytes(raw)
            with self.assertRaises(SettingsError):
                load_settings(self.workspace, {"VIBENOVEL_API_BASE": "https://valid.invalid/v1",
                              "VIBENOVEL_API_MODEL": "model", "VIBENOVEL_API_KEY": "env-secret"})

    def test_url_embedded_secrets_are_never_exposed_and_loopback_is_explicit(self):
        for url in ("https://user:url-private-secret@example.invalid/v1", "https://example.invalid/v1?key=url-private-secret"):
            self.write(self.config(base_url=url))
            state = capabilities({}, workspace=self.workspace)
            self.assertFalse(state["api"]["available"])
            self.assertNotIn("url-private-secret", json.dumps(state))
            with self.assertRaises(ProviderError) as error:
                OpenAICompatibleProvider({}, workspace=self.workspace)
            self.assertNotIn("url-private-secret", str(error.exception))
        self.write(self.config(base_url="http://127.0.0.1:12345/v1"))
        self.assertFalse(capabilities({}, workspace=self.workspace)["api"]["available"])
        self.write(self.config(base_url="http://127.0.0.1:12345/v1", allow_loopback=True))
        self.assertTrue(capabilities({}, workspace=self.workspace)["api"]["available"])
        self.assertFalse(capabilities({"VIBENOVEL_ALLOW_LOOPBACK_API": "0"}, workspace=self.workspace)["api"]["available"])

    def test_linked_config_directory_is_rejected_without_reading_target(self):
        self.path.parent.rmdir()
        outside = self.workspace / "outside-config"
        outside.mkdir()
        (outside / self.path.name).write_text(json.dumps(self.config()), encoding="utf-8")
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(self.path.parent), str(outside)],
                                    capture_output=True, check=False)
            if result.returncode:
                self.skipTest("Junction unavailable")
            self.addCleanup(lambda: os.rmdir(self.path.parent))
        else:
            self.path.parent.symlink_to(outside, target_is_directory=True)
            self.addCleanup(self.path.parent.unlink)
        with self.assertRaises(SettingsError):
            load_settings(self.workspace, {})


class _APIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"choices":[{"message":{"content":"local candidate"}}],"usage":{"total_tokens":5}}')


class LocalSettingsRuntimeTests(unittest.TestCase):
    setUp = LocalSettingsTests.setUp
    config = LocalSettingsTests.config
    write = LocalSettingsTests.write
    def test_manager_reloads_file_for_capabilities_and_each_real_provider_without_leaking_key(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _APIHandler)
        server.received = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        project = ProjectStore(self.workspace).init("test-story", genre="campus")
        document = DocumentStore(project).save("outlines/source/ch001.md", "source", None)
        manager = JobManager(self.workspace)
        self.addCleanup(manager.close)
        with patch.dict(os.environ, {}, clear=True):
            manager.start()
            self.assertEqual(manager.capabilities()["default_provider"], "api")
            self.assertFalse(manager.capabilities()["api"]["available"])
            for secret, model in (("first-private-secret", "first-model"), ("next-private-secret", "next-model")):
                self.write(self.config(base_url="http://127.0.0.1:%s/v1" % server.server_port,
                                       api_key=secret, model=model, allow_loopback=True))
                self.assertTrue(manager.capabilities()["api"]["available"])
                job = manager.submit(project, {"kind": "expand_outline", "provider": "api", "document_path": document["path"],
                                               "document_hash": document["hash"], "instruction": "expand"})
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    final = manager.get(project, job["id"])
                    if final["state"] in ("succeeded", "failed"):
                        break
                    time.sleep(.01)
                self.assertEqual(final["state"], "succeeded", final)
                self.assertEqual(server.received[-1][0], "Bearer " + secret)
                self.assertEqual(server.received[-1][1]["model"], model)
                stored = project.path(".runtime/jobs/" + job["id"] + ".json").read_text(encoding="utf-8")
                self.assertNotIn(secret, stored + json.dumps(final) + json.dumps(manager.capabilities()))
            self.path.write_bytes(b'{"secret-malicious-field":1,"secret-malicious-field":2}')
            self.assertFalse(manager.capabilities()["api"]["available"])
            job = manager.submit(project, {"kind": "expand_outline", "provider": "api", "document_path": document["path"],
                                           "document_hash": document["hash"]})
            until = time.monotonic() + 5
            while time.monotonic() < until:
                final = manager.get(project, job["id"])
                if final["state"] == "failed":
                    break
                time.sleep(.01)
            self.assertEqual(final["state"], "failed")
            self.assertEqual(len(server.received), 2)
            self.assertNotIn("secret-malicious-field", json.dumps(final))


if __name__ == "__main__":
    unittest.main()

"""Offline transport, capability and process-boundary contracts."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reels.editor.claude import ClaudeCLI, SEARCH_MODEL, account_environment, classify_error
from reels.editor.http import CapabilityServer
from reels.editor.jobs import JobContext, JobManager
from reels.editor.protocol import MAX_MESSAGE, ProtocolError, Writer, decode, respond
from reels.editor.relay import Relay, call_relay


class ProtocolTests(unittest.TestCase):
    def test_round_trip_and_validation(self):
        request = {"version": 1, "id": "a", "method": "read", "params": {"a": 3}}
        self.assertEqual(respond(request, lambda method, params: params)["result"], {"a": 3})
        self.assertEqual(respond({**request, "version": 2}, lambda *args: 4)["error"]["code"], "protocol_version")
        self.assertEqual(respond({**request, "params": []}, lambda *args: 4)["error"]["code"], "invalid_request")

    def test_nonfinite_and_invalid_json(self):
        for line in ('{"value":NaN}', '[]', 'nonsense'):
            with self.assertRaises(ProtocolError):
                decode(line)
        with self.assertRaises(ProtocolError):
            decode(" " * (MAX_MESSAGE + 1))

    def test_parallel_event_sequences_are_monotonic(self):
        output = io.StringIO()
        writer = Writer(output)
        threads = [threading.Thread(target=lambda: [writer.event("test", {}) for _ in range(15)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([value["sequence"] for value in values], list(range(1, 61)))

    def test_structured_failure(self):
        result = respond({"version": 1, "id": 1, "method": "edit"},
                         lambda *args: (_ for _ in ()).throw(ProtocolError("conflict", "stale", {"revision": "b"})))
        self.assertEqual(result["error"]["details"], {"revision": "b"})

    def test_launcher_never_sources_dotenv_for_editor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            launcher = root / "reels"
            shutil.copy2(Path(__file__).resolve().parents[1] / "reels", launcher)
            (root / ".env").write_text("echo PRIVATE_ENV_WAS_EVALUATED\n")
            for args in (["editor", "serve"], ["--editor-workspace", "/private", "editor", "serve"],
                         ["--editor-workspace=/private", "editor", "serve"]):
                result = subprocess.run([str(launcher), *args], text=True, capture_output=True, cwd=root)
                self.assertNotIn("PRIVATE_ENV_WAS_EVALUATED", result.stdout + result.stderr)
            ordinary = subprocess.run([str(launcher), "doctor"], text=True, capture_output=True, cwd=root)
            self.assertIn("PRIVATE_ENV_WAS_EVALUATED", ordinary.stdout)


class CapabilityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media.mp4"
        self.media.write_bytes(b"0123456789")
        self.server = CapabilityServer()
        self.addCleanup(self.server.close)
        self.url = self.server.register_file("revision/media.mp4", self.media)

    def request(self, url=None, **headers):
        return urllib.request.urlopen(urllib.request.Request(url or self.url, headers=headers), timeout=3)

    def test_full_and_head(self):
        with self.request() as response:
            self.assertEqual(response.read(), b"0123456789")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        with urllib.request.urlopen(urllib.request.Request(self.url, method="HEAD"), timeout=3) as response:
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], "10")

    def test_byte_ranges(self):
        for value, expected in (("bytes=2-5", b"2345"), ("bytes=7-", b"789"), ("bytes=-3", b"789")):
            with self.request(Range=value) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
        for value in ("bytes=99-", "bytes=4-2", "bytes=1-2,4-5", "bytes=-0"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(Range=value)
            self.assertEqual(error.exception.code, 416)

    def test_capability_traversal_and_mutation_denied(self):
        for url in (self.server.origin + "/revision/media.mp4",
                    self.server.origin + self.server.prefix + "../media.mp4",
                    self.server.origin + self.server.prefix + "%2e%2e/media.mp4",
                    self.server.origin + self.server.prefix + "unregistered"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(url)
            self.assertEqual(error.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(self.url, data=b"x", method="POST"), timeout=3)
        self.assertEqual(error.exception.code, 405)
        self.assertEqual(self.media.read_bytes(), b"0123456789")

    def test_replaced_original_does_not_serve_substitute(self):
        replacement = self.root / "replacement"
        replacement.write_bytes(b"fake")
        os.replace(replacement, self.media)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request()
        self.assertEqual(error.exception.code, 409)

    def test_immutable_registration_and_symlinks(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"other")
        with self.assertRaises(ProtocolError):
            self.server.register_file("revision/media.mp4", other)
        symlink = self.root / "link"
        symlink.symlink_to(other)
        with self.assertRaises(ProtocolError):
            self.server.register_file("link", symlink)


class JobTests(unittest.TestCase):
    def test_stale_result_cannot_publish(self):
        with tempfile.TemporaryDirectory() as td:
            revisions = {"document": "a"}
            entered, release, complete = threading.Event(), threading.Event(), threading.Event()
            published = []
            def emit(name, payload, rev):
                if payload.get("state") in ("completed", "superseded", "failed"):
                    complete.set()
            manager = JobManager(Path(td), emit, lambda: dict(revisions))
            try:
                def work(ctx):
                    entered.set()
                    release.wait(5)
                    return {"movie": "old"}
                job = manager.submit("preview", revisions, work, published.append)
                self.assertTrue(entered.wait(3))
                revisions["document"] = "b"
                release.set()
                self.assertTrue(complete.wait(3))
                self.assertEqual(manager.status(job["id"])["state"], "superseded")
                self.assertEqual(published, [])
            finally:
                release.set()
                manager.close()

    def test_interrupted_jobs_do_not_become_exports(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.json"
            path.write_text(json.dumps({"id": "a", "kind": "export", "state": "running"}))
            manager = JobManager(Path(td), lambda *args: None, lambda: {})
            try:
                self.assertEqual(manager.status("a")["state"], "interrupted")
            finally:
                manager.close()

    def test_cancel_owned_process_group(self):
        context = JobContext()
        errors = []
        started = threading.Event()
        def run():
            started.set()
            try:
                context.run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=Path.cwd(), timeout=70)
            except ProtocolError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        context.stop()
        thread.join(timeout=6)
        self.assertFalse(thread.is_alive())
        self.assertIn("cancelled", errors)


class RelayAndClaudeTests(unittest.TestCase):
    def test_relay_permissions_and_private_files(self):
        relay = Relay(lambda method, params, scope: {"method": method, "scope": scope})
        try:
            result = call_relay(relay.path, relay.token_files["search"], "index.get", {"ids": []})
            self.assertEqual(result["scope"], "search")
            self.assertEqual(relay.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(relay.path.stat().st_mode & 0o777, 0o600)
            os.chmod(relay.token_files["search"], 0o644)
            with self.assertRaises(ProtocolError):
                call_relay(relay.path, relay.token_files["search"], "index.get")
        finally:
            relay.close()

    def test_cli_flags_and_no_api_billing_environment(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = SimpleNamespace(root=Path(td), data_root=Path(td) / "data", code_root=Path.cwd())
            relay = Relay(lambda *args: {})
            cli = ClaudeCLI(workspace, relay, lambda *args: None)
            try:
                with patch("shutil.which", return_value="/usr/local/bin/claude"):
                    editing = cli.arguments("trim the first shot")
                    search = cli.arguments("roadside", search=True)
                for required in ("--restricted", "--strict-mcp-config", "--no-chrome",
                                 "--disable-slash-commands", "--include-partial-messages", "--permission-prompts"):
                    self.assertIn(required, editing)
                self.assertEqual(editing[editing.index("--tools") + 1], "")
                self.assertEqual(editing[editing.index("--permission-prompts") + 1], "none")
                self.assertNotIn("--bare", editing)
                self.assertNotIn("--dangerously-skip-permissions", editing)
                self.assertNotIn("--model", editing)
                self.assertIn(SEARCH_MODEL, search)
                self.assertIn("--no-session-persistence", search)
                allowlist = search[search.index("--allowed-tools") + 1]
                self.assertNotIn("apply_operations", allowlist)
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "do-not-inherit", "OPENAI_API_KEY": "neither"}):
                    env = account_environment()
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("OPENAI_API_KEY", env)
            finally:
                cli.close()
                relay.close()

    def test_specific_failure_categories(self):
        self.assertEqual(classify_error("429 rate limit reached"), "rate_limited")
        self.assertEqual(classify_error("please login"), "authentication_required")
        self.assertEqual(classify_error("requested model is not available"), "model_unavailable")
        self.assertEqual(classify_error("connection timed out"), "network_error")
        self.assertEqual(classify_error("Error querying model: network connection timed out"), "network_error")

    def test_official_mcp_stdio_tool_inventory(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        relay = Relay(lambda *args: {})
        try:
            async def exercise():
                code = ("from reels.editor.mcp import serve_mcp; "
                        f"serve_mcp({str(relay.path)!r}, {str(relay.token_files['search'])!r}, 'search')")
                params = StdioServerParameters(command=sys.executable, args=["-c", code],
                                               env={**os.environ, "PYTHONPATH": str(Path.cwd() / "scripts")})
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertEqual({tool.name for tool in tools.tools},
                                         {"catalog", "search_passages", "get_passages"})
            asyncio.run(exercise())
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()

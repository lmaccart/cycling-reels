"""Live editor service on a throwaway repo: plans, import/ingest, context and the no-delete guarantee."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from helpers import TempProject, live_workspace
from reels.editor import live
from reels.editor.protocol import ProtocolError
from reels.editor.service import EditorService
from reels.util import write_json

SANDBOX = platform.system() == "Darwin" and shutil.which("sandbox-exec")
EDITOR = Path(__file__).resolve().parents[1] / "scripts/reels/editor"


def tree_digest(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


class DestructiveCallAuditTests(unittest.TestCase):
    """Every delete/rename/replace in the editor package was reviewed: none may target library/ or plans/.

    Add to this table only after confirming the new call works on private editor state, a
    scratch/job folder, out/<slug>/ or inbox/ temporaries, never on library/ or plans/.
    The benchmark alone removes its own newly created zz-bench-* plan."""

    EXPECTED = {
        "bench.py": {"rmtree": 1, "unlink": 3},           # its own temporary plan, sidecar, compiled cache and guarded history
        "compile_runtime.py": {"unlink": 2, "rmtree": 1}, # copied/overridden/generated inputs in private scratch only
        "compile_worker.py": {"remove": 2},             # list.remove on worker objects, never filesystem paths
        "exporting.py": {"unlink": 2, "replace": 1},      # save-as temp files next to the chosen destination
        "jobs.py": {"replace": 1, "unlink": 1},            # atomic write of private job records
        "live.py": {"unlink": 1},                          # the import's own .part temp inside inbox/
        "preview.py": {"replace": 2, "rmtree": 10, "unlink": 2},  # private jobs/exports and out/<slug>; document.json uses exclusive temp creation + fd chmod + atomic replace, never writes through worker links
        "preview_cache.py": {"rename": 1, "rmdir": 1, "unlink": 2},  # preview revisions (including renderer drafts) + cache only: shared worker/exclusive trim lock, refreshed protections, fd-relative no-follow deletion; never library/, plans/, proxies or reservations
        "proxy_cache.py": {"unlink": 2},                   # reservation leases under preview/proxy-reservations only
        "proxy_metal.py": {"unlink": 6},                   # this locked proxy job's native/mux/request temporaries only
        "proxy_stabilize.py": {"replace": 1, "unlink": 1},  # CORI JSON and its own temporary in the private proxy cache
        "relay.py": {"unlink": 3, "rmdir": 1},             # this launch's capability files
        "renderer.py": {"rmtree": 2, "unlink": 1},         # candidate renderer snapshots under .cache/editor/live
        "service.py": {"unlink": 1},                       # stale .source-/.sphere- partials in preview/sources|spheres only
        "transactions.py": {"replace": 1, "unlink": 2, "remove": 1},  # temp files, scratch clone, guarded _delete_private
        "worker.py": {"replace": 6, "rmtree": 2, "unlink": 5},  # sandbox-owned jobs/proxies; unlink also half-restored products and superseded render.mp4; read-only scratch uses preview_cache.remove_tree's no-follow deletion
        "document.py": {"remove": 1},
    }
    NAMES = {"unlink", "rmtree", "remove", "rename", "renames", "rmdir", "move", "removedirs", "truncate", "replace"}

    def test_destructive_calls_match_the_reviewed_table(self):
        found = {}
        for path in sorted(EDITOR.glob("*.py")):
            counts: dict[str, int] = {}
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in self.NAMES:
                    if node.func.attr == "replace" and not (isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                        continue
                    counts[node.func.attr] = counts.get(node.func.attr, 0) + 1
            if counts:
                found[path.name] = counts
        self.assertEqual(found, self.EXPECTED)

    def test_journal_deletion_refuses_library_and_plans(self):
        from reels.editor.transactions import DocumentStore
        fixture = TempProject(self)
        fixture.asset("a")
        fixture.plan("p", [{"asset": "a", "in": 0, "out": 2}])
        workspace = live_workspace(fixture)
        victim = fixture.root / "library/assets/a.json"
        plan = fixture.root / "plans/p.json"
        store = DocumentStore.__new__(DocumentStore)
        store.data_root = fixture.root.resolve()
        for relative in ("library/assets/a.json", "plans/p.json", "library/media/a.mp4"):
            store._delete_private(relative)
        self.assertTrue(victim.is_file() and plan.is_file() and (fixture.root / "library/media/a.mp4").is_file())
        (fixture.root / "music").mkdir(exist_ok=True)
        scratch = fixture.root / "music/x.json"
        scratch.write_text("{}")
        store._delete_private("music/x.json")
        self.assertFalse(scratch.exists())
        self.assertIsNotNone(workspace)


@unittest.skipUnless(SANDBOX, "macOS sandbox-exec is required for the renderer compile worker")
class LiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("a")
        self.repo = self.fixture.root.resolve()
        self.workspace = live_workspace(self.fixture)
        self.events: list[tuple] = []
        self.service = EditorService(self.workspace, lambda *args, **kwargs: self.events.append((args, kwargs)))
        self.addCleanup(self.service.close)
        self.media_before = tree_digest(self.repo / "library/media")
        self.sources = Path(tempfile.mkdtemp(prefix="reels-import-source-"))
        self.addCleanup(shutil.rmtree, self.sources, True)

    def names(self):
        return [args[0] for args, _ in self.events]

    def wait_job(self, job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.service.dispatch("job.status", {"id": job_id})
            if status["state"] not in ("queued", "running"):
                return status
            time.sleep(0.05)
        self.fail("job did not finish")

    def stub_reels(self, body="echo ingesting; echo done"):
        script = self.repo / "reels"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)

    # -- plans -----------------------------------------------------------

    def test_create_open_edit_undo_leaves_originals_byte_identical(self):
        created = self.service.dispatch("projects.create", {"title": "Little Gray!", "postDate": "2026-11-15"})
        self.assertEqual(created, {"path": "plans/2026-11-15-little-gray.json"})
        plan_file = self.repo / created["path"]
        self.assertEqual(json.loads(plan_file.read_text()),
                         {"slug": "2026-11-15-little-gray", "postDate": "2026-11-15", "hook": {"text": ""}, "segments": []})
        opened = self.service.dispatch("document.open", {"path": created["path"]})
        self.assertEqual(opened["durationFrames"], 0)
        self.assertEqual(self.service.dispatch("preview.prepare", {})["state"], "empty")
        with self.assertRaises(ProtocolError) as empty:
            self.service.dispatch("export.start", {})
        self.assertEqual(empty.exception.code, "empty_plan")
        applied = self.service.dispatch("document.apply", {
            "expected": opened["revisions"], "reason": "add clip",
            "operations": [{"op": "insert_segment", "value": {"asset": "a", "in": 0, "out": 2}}]})
        self.assertEqual(applied["durationFrames"], 60)
        # The real plan file changed, not a private copy.
        self.assertEqual(json.loads(plan_file.read_text())["segments"][0]["asset"], "a")
        # The native app wraps the batch: {"batch": {...}} (EditorStore.apply).
        wrapped = self.service.dispatch("document.apply", {"batch": {
            "expected": applied["revisions"], "reason": "trim", "origin": "manual",
            "operations": [{"op": "trim_segment", "id": applied["nodes"][0]["id"], "sourceIn": 0, "sourceOut": 1}]}})
        self.assertEqual(wrapped["durationFrames"], 30)
        self.service.dispatch("document.undo", {})
        undone = self.service.dispatch("document.undo", {})
        self.assertEqual(undone["durationFrames"], 0)
        self.assertEqual(json.loads(plan_file.read_text())["segments"], [])
        self.service.dispatch("document.redo", {})
        self.assertEqual(json.loads(plan_file.read_text())["segments"][0]["asset"], "a")
        self.assertEqual(tree_digest(self.repo / "library/media"), self.media_before)
        self.assertTrue((self.repo / "library/assets/a.json").is_file())

    def test_projects_create_never_overwrites_and_defaults(self):
        # A past date, so the explicit plans can never collide with the "today" default below.
        first = self.service.dispatch("projects.create", {"title": "Same", "postDate": "2026-01-02"})["path"]
        second = self.service.dispatch("projects.create", {"title": "Same", "postDate": "2026-01-02"})["path"]
        third = self.service.dispatch("projects.create", {"postDate": "2026-01-02"})["path"]
        self.assertEqual([first, second, third], ["plans/2026-01-02-same.json", "plans/2026-01-02-same-2.json",
                                                  "plans/2026-01-02-untitled.json"])
        today = self.service.dispatch("projects.create", {})["path"]
        self.assertEqual(today, f"plans/{live.today(self.service.project)}-untitled.json")
        with self.assertRaises(ProtocolError):
            self.service.dispatch("projects.create", {"postDate": "tomorrow"})

    def test_plans_list_is_extended_and_sorted_newest_first(self):
        cover = self.repo / "out/newer/cover.jpg"
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b"jpg")
        write_json(self.repo / "out/newer/post.json", {"slug": "newer", "status": "approved"})
        write_json(self.repo / "plans/newer.json", {
            "slug": "newer", "postDate": "2026-11-16", "format": "story", "hook": {"text": "big hook"},
            "segments": [{"asset": "a", "in": 1, "out": 3}, {"asset": "a", "in": 0, "out": 4, "speed": 2}]})
        write_json(self.repo / "plans/older.json", {"slug": "older", "postDate": "2026-11-01", "segments": []})
        (self.repo / "plans/broken.json").write_text("{nope")
        plans = self.service.dispatch("plans.list", {})["plans"]
        self.assertEqual([p["slug"] for p in plans][:2], ["newer", "older"])
        newer = plans[0]
        self.assertEqual((newer["title"], newer["postDate"], newer["format"], newer["segmentCount"]),
                         ("big hook", "2026-11-16", "story", 2))
        self.assertEqual(newer["durationSeconds"], 4.0)
        self.assertEqual(newer["status"], "approved")
        self.assertTrue(newer["thumbnailURL"].startswith("http://127.0.0.1:"))
        self.assertIsInstance(newer["modified"], float)
        older = plans[1]
        self.assertEqual((older["title"], older["status"], older["thumbnailURL"], older["durationSeconds"]),
                         ("older", "draft", None, None))
        self.assertTrue(next(p for p in plans if p["slug"] == "broken")["invalid"])

    def test_plans_changed_event_fires_for_external_edits(self):
        self.service._poll_plans()
        write_json(self.repo / "plans/external.json", {"slug": "external", "segments": []})
        self.service._poll_plans()
        event = [args for args, _ in self.events if args[0] == "plans.changed"][-1]
        self.assertIn("plans/external.json", event[1]["paths"])
        self.assertEqual(event[1]["added"], ["plans/external.json"])

    def test_context_json_tracks_the_open_plan_and_selection(self):
        created = self.service.dispatch("projects.create", {"title": "ctx", "postDate": "2026-11-15"})["path"]
        snapshot = self.service.dispatch("document.open", {"path": created})
        context = json.loads((self.workspace.root / "context.json").read_text())
        self.assertEqual((context["plan"], context["selection"]), (created, []))
        node = snapshot["nodes"][0]["id"]
        self.service.dispatch("selection.set", {"ids": [node], "playhead": 45})
        context = json.loads((self.workspace.root / "context.json").read_text())
        self.assertEqual(context["selection"][0]["id"], node)
        self.assertEqual(context["playheadSeconds"], 1.5)
        self.assertIn("updatedAt", context)

    def test_renderer_submit_is_disabled_in_live_mode(self):
        with self.assertRaises(ProtocolError) as error:
            self.service.dispatch("renderer.submit", {"changes": {}})
        self.assertEqual(error.exception.code, "renderer_changes_unavailable")
        self.assertIn("Claude Code in the terminal", error.exception.message)

    # -- footage import -----------------------------------------------------

    def make_source_tree(self):
        (self.sources / "ride/sub").mkdir(parents=True)
        files = {
            "ride/GX010001.MP4": b"video-one",
            "ride/GX010001.LRV": b"low res",
            "ride/GX010001.THM": b"thumb",
            "ride/.hidden.mp4": b"hidden",
            "ride/readme.pdf": b"notes",
            "ride/sub/IMG_1.jpg": b"photo",
            "single.mov": b"single",
        }
        for name, data in files.items():
            (self.sources / name).write_bytes(data) if (self.sources / name).parent.exists() else None
        return files

    def test_import_copies_only_supported_media_and_never_touches_sources(self):
        self.make_source_tree()
        before = tree_digest(self.sources)
        self.stub_reels()
        result = self.service.dispatch("library.import", {
            "paths": [str(self.sources / "ride"), str(self.sources / "single.mov")], "ingest": False})
        self.assertEqual(result["copied"], 3)
        self.assertTrue(any("readme.pdf" in item for item in result["skipped"]))
        job = self.wait_job(result["jobId"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(sorted(p.name for p in (self.repo / "inbox").iterdir()),
                         ["GX010001.MP4", "IMG_1.jpg", "single.mov"])
        self.assertEqual(tree_digest(self.sources), before, "sources must be untouched")
        self.assertEqual(tree_digest(self.repo / "library/media"), self.media_before)
        (self.repo / "inbox" / "README.md").write_text("drop files here")
        inbox = self.service.dispatch("library.inbox", {})
        self.assertEqual(inbox["count"], 3, "README and other non-media files are not counted")
        (self.repo / "inbox" / "README.md").unlink()
        # Importing the same name again never replaces the earlier copy.
        again = self.service.dispatch("library.import", {"paths": [str(self.sources / "single.mov")], "ingest": False})
        self.wait_job(again["jobId"])
        self.assertEqual(sorted(p.name for p in (self.repo / "inbox").iterdir() if p.name.startswith("single")),
                         ["single (2).mov", "single.mov"])
        self.assertEqual(tree_digest(self.sources), before)

    def test_import_then_ingest_streams_log_and_announces_library_changed(self):
        self.make_source_tree()
        self.stub_reels("echo scanning inbox; echo wrote asset; exit 0")
        result = self.service.dispatch("library.import", {"paths": [str(self.sources / "single.mov")]})
        job = self.wait_job(result["jobId"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["kind"], "ingest")
        logs = [args[1].get("log") for args, _ in self.events if args[0] == "job.changed" and args[1].get("log")]
        self.assertIn("scanning inbox", logs)
        self.assertIn("wrote asset", logs)
        self.assertIn("library.changed", self.names())
        self.assertEqual(tree_digest(self.repo / "library/media"), self.media_before)

    def test_ingest_failure_is_reported_and_cancel_stops_the_subprocess(self):
        self.stub_reels("echo broke >&2; exit 3")
        failed = self.wait_job(self.service.dispatch("library.ingest", {})["jobId"])
        self.assertEqual(failed["state"], "failed")
        self.assertNotIn("library.changed", self.names())
        self.stub_reels("echo started; sleep 30")
        running = self.service.dispatch("library.ingest", {})["jobId"]
        deadline = time.time() + 10
        while not any(a[0] == "job.changed" and a[1].get("log") == "started" for a, _ in self.events):
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        with self.assertRaises(ProtocolError) as busy:
            self.service.dispatch("library.ingest", {})
        self.assertEqual(busy.exception.code, "ingest_running")
        self.service.dispatch("job.cancel", {"id": running})
        self.assertEqual(self.wait_job(running)["state"], "cancelled")

    def test_import_rejects_library_paths_and_bad_input(self):
        with self.assertRaises(ProtocolError):
            self.service.dispatch("library.import", {"paths": []})
        files, skipped = live.plan_import(self.service.project, [str(self.repo / "library/media/a.mp4"), "relative.mp4"])
        self.assertEqual(files, [])
        self.assertEqual(len(skipped), 2)

    # -- export publishing --------------------------------------------------

    def test_export_publish_lands_in_out_slug_and_keeps_history(self):
        from reels.editor.preview import PreviewManager
        manager = PreviewManager(self.workspace)
        slug = "2026-11-15-pub"
        write_json(self.repo / f"out/{slug}/post.json", {"slug": slug, "status": "approved", "history": ["x"]})
        (self.repo / f"out/{slug}/preview.mp4").write_bytes(b"stale")
        data = self.workspace.root / "exports/e1/data"
        manager._seed_previous_post({"plan": {"slug": slug}}, data)
        self.assertEqual(json.loads((data / f"out/{slug}/post.json").read_text())["history"], ["x"])
        produced = self.workspace.root / "exports/e1/data/out" / slug
        for name, body in (("reel.mp4", b"reel"), ("cover.jpg", b"cover"), ("post.json", b'{"status": "rendered"}')):
            (produced / name).write_bytes(body)
        result = manager._publish_to_out({"slug": slug, "exportRoot": str(self.workspace.root / "exports/e1")}, produced)
        self.assertEqual((self.repo / f"out/{slug}/reel.mp4").read_bytes(), b"reel")
        self.assertEqual(json.loads((self.repo / f"out/{slug}/post.json").read_text())["status"], "rendered")
        self.assertFalse((self.repo / f"out/{slug}/preview.mp4").exists(), "superseded review movie is replaced")
        self.assertEqual(result["reelPath"], str(self.repo / f"out/{slug}/reel.mp4"))
        self.assertEqual(sorted(p.name for p in (self.repo / "plans").glob("*")), [])
        self.assertEqual(tree_digest(self.repo / "library/media"), self.media_before)


@unittest.skipUnless(SANDBOX and shutil.which("ffmpeg") and os.environ.get("REELS_RUN_SANDBOX_RENDER_PROOF") == "1",
                     "set REELS_RUN_SANDBOX_RENDER_PROOF=1 (needs ffmpeg, node, sandbox-exec)")
class LiveExportProof(unittest.TestCase):
    def test_export_renders_the_live_plan_into_out_slug_without_touching_library_media(self):
        import subprocess
        fixture = TempProject(self)
        media = fixture.project.media_dir / "proof.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=24",
                        "-t", "2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)
        fixture.asset("proof", duration=2.0, width=360, height=640, audio=False, media=media)
        fixture.plan("proof-plan", [{"asset": "proof", "in": 0, "out": 2}], postDate="2026-11-15", hook={"text": "hi"})
        workspace = live_workspace(fixture)
        # The proof needs the real remotion install for rendering.
        real = Path(__file__).resolve().parents[1] / "remotion"
        (fixture.root / "remotion").rmdir()
        (fixture.root / "remotion").symlink_to(real, target_is_directory=True)
        before = tree_digest(fixture.root / "library/media")
        service = EditorService(workspace, lambda *a, **k: None)
        self.addCleanup(service.close)
        service.dispatch("document.open", {"path": "plans/proof-plan.json"})
        job = service.dispatch("export.start", {})
        deadline = time.time() + 600
        while time.time() < deadline:
            status = service.dispatch("job.status", {"id": job["id"]})
            if status["state"] not in ("queued", "running"):
                break
            time.sleep(1)
        self.assertEqual(status["state"], "completed", status.get("error"))
        out = fixture.root / "out/proof-plan"
        self.assertTrue((out / "reel.mp4").is_file())
        self.assertEqual(json.loads((out / "post.json").read_text())["status"], "rendered")
        self.assertEqual(status["result"]["reelPath"], str(out.resolve() / "reel.mp4"))
        self.assertEqual(tree_digest(fixture.root / "library/media"), before)


class ClaudeInLiveModeTests(unittest.TestCase):
    def test_chat_is_tool_restricted_and_runs_from_the_repo(self):
        from reels.editor.claude import ClaudeCLI
        from reels.editor.relay import Relay
        fixture = TempProject(self)
        (fixture.root / "remotion").mkdir()
        workspace = live_workspace(fixture)
        relay = Relay(lambda *args: None)
        self.addCleanup(relay.close)
        cli = ClaudeCLI(workspace, relay, lambda *a, **k: None)
        fake = fixture.root / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        from unittest import mock
        with mock.patch("reels.editor.claude.find_claude", return_value=str(fake)):
            args = cli.arguments("hi")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--restricted", args)
        allowed = args[args.index("--allowed-tools") + 1]
        self.assertTrue(all(name.startswith("mcp__reels_editor__") for name in allowed.split(",")))
        self.assertNotIn("Bash", allowed)
        config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
        self.assertNotIn("--editor-workspace", config["mcpServers"]["reels_editor"]["args"])
        self.assertEqual(config["mcpServers"]["reels_editor"]["command"], str(workspace.code_root / ".venv/bin/python"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import platform
import shutil
import unittest
from types import SimpleNamespace

from helpers import TempProject
from reels.editor.compile_worker import begin_compile_session, WorkerCancelled
from reels.editor.transactions import DocumentStore, DocumentError


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec"), "macOS sandbox required")
class SharedCompileSessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("a")
        self.workspace = SimpleNamespace(project=self.fixture.project, root=self.fixture.root)

    def store(self, name):
        self.fixture.plan(name, [{"asset": "a", "in": 0, "out": 5}])
        store = DocumentStore(self.workspace, f"plans/{name}.json")
        self.addCleanup(store.close)
        return store

    def test_broker_session_reuses_worker_after_closing_and_opening_documents(self):
        session = begin_compile_session(self.workspace)
        self.addCleanup(session.close)
        first = self.store("first")
        pool = first._warm_pool
        process = pool.workers[0].process
        first.close()
        self.assertFalse(pool.closed)
        second = self.store("second")
        third = self.store("third")
        self.assertIs(second._warm_pool, pool)
        self.assertIs(third._warm_pool, pool)
        self.assertEqual(len(pool.workers), 1)
        self.assertIs(pool.workers[0].process, process)
        self.assertLessEqual(len(pool.workers), pool.max_workers)

    def test_closing_one_document_does_not_cancel_other_document(self):
        session = begin_compile_session(self.workspace)
        self.addCleanup(session.close)
        first, second = self.store("first"), self.store("second")
        pool, process = second._warm_pool, second._warm_pool.workers[0].process
        first.cancel_pending("all")
        first.close()
        snapshot = second.snapshot()
        result = second.apply({"expected": snapshot["revisions"], "origin": "manual",
                               "operations": [{"op": "set_field", "path": "/note", "value": "valid"}]})
        self.assertEqual(result["plan"]["note"], "valid")
        self.assertIs(pool.workers[0].process, process)

    def test_restart_never_reattaches_old_documents_to_the_new_owner(self):
        old_session = begin_compile_session(self.workspace)
        old_store = self.store("old")
        old_pool = old_store._warm_pool
        old_session.close()
        self.assertTrue(old_pool.closed)
        new_session = begin_compile_session(self.workspace)
        self.addCleanup(new_session.close)
        new_store = self.store("new")
        self.assertIsNot(new_store._warm_pool, old_pool)
        with self.assertRaises(WorkerCancelled):
            _ = old_store._compile_pool_lease.pool
        with self.assertRaises(DocumentError) as closed:
            old_store.snapshot()
        self.assertEqual(closed.exception.code, "closed")
        old_store.close()
        self.assertFalse(new_store._warm_pool.closed)
        self.assertEqual(new_store.snapshot()["durationFrames"], 150)

    def test_standalone_last_document_release_leaves_no_workers(self):
        first, second = self.store("first"), self.store("second")
        pool = first._warm_pool
        self.assertIs(pool, second._warm_pool)
        first.close()
        self.assertFalse(pool.closed)
        second.close()
        self.assertTrue(pool.closed)
        self.assertEqual(pool.workers, [])

    def test_failed_open_releases_its_document_lease_immediately(self):
        with self.assertRaises(DocumentError):
            DocumentStore(self.workspace, "plans/missing.json")
        self.assertTrue(self.workspace._editor_compile_owner.closed)
        self.assertEqual(self.workspace._editor_compile_owner.documents, 0)

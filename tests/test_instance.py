"""Real process ownership of local trading state; no brokerage access."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.instance import InstanceInUseError, project_identity
from app.storage import Store

ROOT = Path(__file__).resolve().parent.parent


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="kiwoom-instance-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data"

    def child(self, code):
        return subprocess.run([sys.executable, "-c", code, str(self.path)], cwd=ROOT,
                              text=True, capture_output=True, timeout=15, check=False)

    def test_second_store_refuses_before_read_or_write_and_snapshot_is_unchanged(self):
        owner = Store(self.path)
        self.addCleanup(owner.close)
        owner.save({"marker": "owner snapshot", "orders": ["durable intent"]})
        with self.assertRaisesRegex(InstanceInUseError, "이미 실행 중"):
            Store(self.path)
        self.assertEqual(owner.load(), {"marker": "owner snapshot", "orders": ["durable intent"]})

    def test_separate_process_cannot_take_a_held_data_directory(self):
        owner = Store(self.path)
        self.addCleanup(owner.close)
        owner.save({"marker": "do not change"})
        result = self.child("""
import sys
from app.storage import Store
from app.instance import InstanceInUseError
try:
    other = Store(sys.argv[1])
except InstanceInUseError:
    raise SystemExit(23)
else:
    other.save({'marker': 'incorrect second owner'})
    other.close()
    raise SystemExit(0)
""")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(owner.load(), {"marker": "do not change"})

    def test_close_releases_for_another_process_and_keeps_lock_file(self):
        owner = Store(self.path)
        lock_path = owner.instance_lock.path
        owner.save({"first": True})
        owner.close()
        owner.close()  # Repeated cleanup is harmless.
        self.assertTrue(lock_path.is_file())
        result = self.child("""
import sys
from app.storage import Store
store = Store(sys.argv[1])
assert store.load() == {'first': True}
store.save({'second': True})
store.close()
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(lock_path.is_file())
        restored = Store(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.load(), {"second": True})

    def test_process_crash_releases_ownership_without_deleting_lock_file(self):
        result = self.child("""
import os, sys
from app.storage import Store
store = Store(sys.argv[1])
store.save({'committed_before_crash': True})
os._exit(17)
""")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertTrue((self.path / ".instance.lock").is_file())
        restored = Store(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.load(), {"committed_before_crash": True})

    def test_canonical_symlink_alias_cannot_bypass_ownership(self):
        owner = Store(self.path)
        self.addCleanup(owner.close)
        alias = Path(self.directory.name) / "alias"
        try:
            alias.symlink_to(self.path, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks require permission on this platform")
        with self.assertRaises(InstanceInUseError):
            Store(alias)
        self.assertEqual(project_identity(alias), project_identity(self.path))

    def test_failed_database_initialization_releases_lock_and_closes_connection(self):
        broken = Mock()
        broken.execute.side_effect = RuntimeError("synthetic SQLite initialization failure")
        with patch("app.storage.sqlite3.connect", return_value=broken):
            with self.assertRaisesRegex(RuntimeError, "initialization failure"):
                Store(self.path)
        broken.close.assert_called_once()
        recovered = Store(self.path)
        self.addCleanup(recovered.close)
        recovered.save({"recovered": True})
        self.assertEqual(recovered.load(), {"recovered": True})

    def test_failed_connection_close_keeps_ownership_until_shutdown_is_established(self):
        owner = Store(self.path)
        original = owner.connection
        broken = Mock()
        broken.close.side_effect = RuntimeError("synthetic close failure")
        owner.connection = broken
        try:
            with self.assertRaisesRegex(RuntimeError, "close failure"):
                owner.close()
            with self.assertRaises(InstanceInUseError):
                Store(self.path)
        finally:
            owner.connection = original
            owner.close()
        next_owner = Store(self.path)
        self.addCleanup(next_owner.close)

    def test_project_identity_is_opaque_stable_for_canonical_root_and_distinct(self):
        self.path.mkdir(parents=True)
        identity = project_identity(self.path)
        self.assertRegex(identity, r"^[0-9a-f]{64}$")
        self.assertEqual(identity, project_identity(self.path / "."))
        self.assertNotEqual(identity, project_identity(self.path.parent / "other"))
        self.assertNotIn(str(self.path), identity)

    def test_invalid_controller_restore_releases_data_ownership(self):
        from app.main import Controller
        fixture = Store(self.path)
        fixture.save({"settings": {"allocation_percent": "0"}})
        fixture.close()
        with self.assertRaises(ValueError):
            Controller(self.path)
        recovered = Store(self.path)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.load()["settings"]["allocation_percent"], "0")

    def test_health_identifies_checkout_and_controller_lock_capability(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        with patch.dict(os.environ, {"BREAKLINE_STARTUP_TOKEN": "local-test-startup-nonce"}), TestClient(create_app(self.path)) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["project_id"], project_identity(ROOT))
            self.assertEqual(health.json()["instance_lock_version"], 1)
            self.assertEqual(health.json()["pid"], os.getpid())
            self.assertEqual(health.json()["startup_token"], "local-test-startup-nonce")
            self.assertIsInstance(health.json()["pid"], int)
            self.assertNotIn("data_dir", health.json())
            self.assertEqual(health.json()["service"], "kiwoom-breakout")
            with self.assertRaises(InstanceInUseError):
                Store(self.path)
        after = Store(self.path)
        self.addCleanup(after.close)

    def test_lifespan_body_error_still_closes_controller(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        with self.assertRaisesRegex(RuntimeError, "synthetic request owner failure"):
            with TestClient(create_app(self.path)):
                raise RuntimeError("synthetic request owner failure")
        after = Store(self.path)
        self.addCleanup(after.close)


class ControllerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.main import Controller
        self.directory = tempfile.TemporaryDirectory(prefix="kiwoom-controller-close-")
        self.controller = Controller(self.directory.name)

    async def asyncTearDown(self):
        # Tests that close through a failing broker still release the store.
        self.controller.store.close()
        self.directory.cleanup()

    async def test_broker_close_failure_after_stopped_workers_releases_store(self):
        broker = Mock()
        broker.close = AsyncMock(side_effect=RuntimeError("synthetic broker close failure"))
        self.controller.broker = broker
        with self.assertRaisesRegex(RuntimeError, "broker close failure"):
            await self.controller.close()
        after = Store(self.directory.name)
        try:
            self.assertIsNotNone(after.load())
        finally:
            after.close()

    async def test_unconfirmed_worker_stop_does_not_release_data_ownership(self):
        original_stop = self.controller.stop
        self.controller.stop = AsyncMock(side_effect=RuntimeError("synthetic worker still active"))
        try:
            with self.assertRaisesRegex(RuntimeError, "worker still active"):
                await self.controller.close()
            with self.assertRaises(InstanceInUseError):
                Store(self.directory.name)
        finally:
            self.controller.stop = original_stop
            await self.controller.close()
        after = Store(self.directory.name)
        after.close()


if __name__ == "__main__":
    unittest.main()

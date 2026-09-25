import base64
import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")

    def test_preservation_proof_lifecycle(self):
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        # setUp 中两次创建副本已各出具一份有效证明
        history = self.store.archive_proof_history("auditor", self.archive["id"])["proofs"]
        self.assertEqual(len(history), 2)
        self.assertTrue(all(p["status"] == "valid" for p in history))
        self.assertEqual(history[0]["healthy_copies"], 2)  # 最新在前
        self.assertTrue(all(p["created_at"] for p in history))

        # 全部副本损坏且没有健康来源 → 新证明失效并写明失效原因
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        self.store.simulate_corruption("owner", self.copy2, "records/one.xml")
        broken = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(broken["state"], "degraded")
        self.assertEqual(broken["proof"]["status"], "invalid")
        self.assertEqual(broken["proof"]["healthy_copies"], 0)
        self.assertIn("无健康副本", broken["proof"]["invalid_reason"])
        current = self.store.archive_proof("auditor", self.archive["id"])["proofs"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["status"], "invalid")

        # 新增健康副本后修复成功 → 出具新的有效证明，旧的失效证明仍可查
        self.store.add_copy("owner", self.version["id"], "offline-disk-c")
        repaired = self.store.verify_copy("owner", self.copy1)
        self.assertTrue(repaired["repaired"])
        self.assertEqual(repaired["proof"]["status"], "valid")
        self.assertEqual(repaired["proof"]["healthy_copies"], 2)
        current = self.store.archive_proof("auditor", self.archive["id"])["proofs"]
        self.assertEqual(current[0]["status"], "valid")
        history = self.store.archive_proof_history("auditor", self.archive["id"])["proofs"]
        self.assertEqual(len(history), 5)
        self.assertTrue(any(p["status"] == "invalid" for p in history))

    def test_proof_access_and_migration_trigger(self):
        # 审计员只能查看有权访问的档案证明
        with self.assertRaises(BusinessError) as ctx:
            self.store.archive_proof("outsider", self.archive["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.archive_proof_history("outsider", self.archive["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 迁移后为生成的新版本出具证明
        migrated = self.store.migrate(
            "owner", self.version["id"], "README.txt", "README.md", "markdown",
            base64.b64encode(b"# readme").decode(),
        )
        self.assertEqual(migrated["proof"]["action"], "format.migrate")
        self.assertEqual(migrated["proof"]["status"], "valid")
        self.assertEqual(migrated["proof"]["total_copies"], 0)
        current = self.store.archive_proof("owner", self.archive["id"])["proofs"]
        self.assertEqual([p["version"] for p in current], [1, 2])


if __name__ == "__main__":
    unittest.main()

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

    def test_preservation_proof_lifecycle_and_history(self):
        # 创建第二个副本后，当前证明有效：2 个健康副本，共出 2 版证明
        proof = self.store.current_proof("owner", self.version["id"])
        self.assertEqual(proof["status"], "valid")
        self.assertEqual(proof["total_copies"], 2)
        self.assertEqual(proof["healthy_copies"], 2)
        self.assertEqual(proof["corrupt_copies"], 0)
        self.assertEqual(proof["trigger"], "copy.create")
        self.assertTrue(proof["verified_at"])
        self.assertEqual(proof["detail"]["failure_reasons"], [])
        self.assertEqual(len(self.store.proof_history("owner", self.version["id"])["proofs"]), 2)

        # 用只有一个副本的新版本演示“没有健康来源”
        version2 = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "solo.txt", "content_b64": base64.b64encode(b"solo").decode()},
        ])
        solo = self.store.add_copy("owner", version2["id"], "solo-disk")
        self.store.simulate_corruption("owner", solo["id"], "solo.txt")
        result = self.store.verify_copy("owner", solo["id"])
        self.assertEqual(result["state"], "corrupt")
        self.assertFalse(result["repaired"])
        bad_proof = self.store.current_proof("owner", version2["id"])
        self.assertEqual(bad_proof["status"], "invalid")
        self.assertEqual(bad_proof["healthy_copies"], 0)
        self.assertTrue(any("没有可用于修复的健康来源" in r for r in bad_proof["detail"]["failure_reasons"]))
        version_view = self.store.get_version("owner", version2["id"])
        self.assertEqual(version_view["version"]["state"], "degraded")
        self.assertEqual(version_view["current_proof"]["status"], "invalid")

        # 补充一个健康副本后修复成功 -> 再出新证明，版本恢复 verified；旧失效证明仍可查
        self.store.add_copy("owner", version2["id"], "solo-disk-backup")
        repaired = self.store.verify_copy("owner", solo["id"])
        self.assertTrue(repaired["repaired"])
        new_proof = self.store.current_proof("owner", version2["id"])
        self.assertEqual(new_proof["status"], "valid")
        self.assertEqual(new_proof["healthy_copies"], 2)
        self.assertEqual(self.store.get_version("owner", version2["id"])["version"]["state"], "verified")
        history = self.store.proof_history("owner", version2["id"])["proofs"]
        self.assertEqual(history[0]["id"], new_proof["id"])
        self.assertTrue(any(p["status"] == "invalid" for p in history))

    def test_proof_after_migration_is_invalid_until_copied(self):
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        proof = self.store.current_proof("owner", migrated["id"])
        self.assertEqual(proof["status"], "invalid")
        self.assertEqual(proof["trigger"], "format.migrate")
        self.assertEqual(proof["total_copies"], 0)
        self.assertTrue(any("尚无任何离线副本" in r for r in proof["detail"]["failure_reasons"]))
        self.store.add_copy("owner", migrated["id"], "migrated-disk-a")
        self.assertEqual(self.store.current_proof("owner", migrated["id"])["status"], "valid")

    def test_proof_access_control_and_archive_view(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.current_proof("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 未获授权的审计员看不到任何证明
        with self.assertRaises(BusinessError) as ctx:
            self.store.archive_proofs("outsider", self.archive["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 给审计员授权后可查看档案内各版本的当前证明
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        view = self.store.archive_proofs("auditor", self.archive["id"])
        self.assertTrue(view["current_proofs"])
        self.assertEqual(view["current_proofs"][0]["proof"]["status"], "valid")


if __name__ == "__main__":
    unittest.main()

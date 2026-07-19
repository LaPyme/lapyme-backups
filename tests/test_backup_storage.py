from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "backup-storage.py"
SPEC = importlib.util.spec_from_file_location("backup_storage", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load backup-storage.py")
backup_storage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_storage
SPEC.loader.exec_module(backup_storage)


def settings() -> backup_storage.Settings:
    return backup_storage.Settings(
        rclone="rclone",
        source_remote="supabase",
        destination_remote="r2",
        destination_bucket="private-bucket",
        destination_prefix="storage/v1",
        buckets=("documents",),
        temp_dir=Path("/tmp"),
    )


def backup_key(bucket: str, seed: str) -> str:
    path_hash = seed[0] * 64
    content_hash = seed[1] * 64
    return f"storage/v1/objects/{bucket}/{path_hash[:2]}/{path_hash}/{content_hash}"


def source_item(path: str, md5: str, size: int = 10) -> dict[str, object]:
    return {
        "Path": path,
        "Size": size,
        "ModTime": "2026-07-18T12:00:00Z",
        "MimeType": "application/pdf",
        "Hashes": {"MD5": md5},
    }


def manifest_item(path: str, md5: str, key: str, size: int = 10) -> dict[str, object]:
    return {
        "path": path,
        "size": size,
        "mod_time": "2026-07-18T12:00:00Z",
        "source_md5": md5,
        "mime_type": "application/pdf",
        "backup_key": key,
    }


class BackupStorageTests(unittest.TestCase):
    def test_valid_backup_key_accepts_only_hashed_private_keys(self) -> None:
        config = settings()
        valid = backup_key("documents", "ab")

        self.assertTrue(
            backup_storage.is_valid_backup_key(config, "documents", valid)
        )
        self.assertFalse(
            backup_storage.is_valid_backup_key(
                config,
                "documents",
                "storage/v1/objects/documents/org/customer/invoice.pdf",
            )
        )

    def test_execute_reuses_unchanged_and_versions_changed_objects(self) -> None:
        config = settings()
        unchanged_key = backup_key("documents", "ab")
        changed_key = backup_key("documents", "cd")
        new_key = backup_key("documents", "ef")
        previous_manifest = {
            "version": 1,
            "buckets": {
                "documents": {
                    "objects": [
                        manifest_item("org/one/invoice.pdf", "same", unchanged_key),
                        manifest_item("org/two/invoice.pdf", "old", backup_key("documents", "ac")),
                        manifest_item("org/deleted/invoice.pdf", "gone", backup_key("documents", "bd")),
                    ]
                }
            },
        }
        source = [
            source_item("org/one/invoice.pdf", "same"),
            source_item("org/two/invoice.pdf", "changed"),
            source_item("org/new/invoice.pdf", "new"),
        ]
        uploaded_manifest: dict[str, object] = {}

        def capture_manifest(
            _settings: backup_storage.Settings, manifest: dict[str, object]
        ) -> str:
            uploaded_manifest.update(manifest)
            return "digest"

        with (
            mock.patch.object(
                backup_storage, "load_latest_manifest", return_value=previous_manifest
            ),
            mock.patch.object(
                backup_storage,
                "list_destination_objects",
                return_value={unchanged_key},
            ),
            mock.patch.object(
                backup_storage,
                "run_rclone",
                return_value=json.dumps(source).encode("utf-8"),
            ),
            mock.patch.object(
                backup_storage,
                "backup_changed_objects",
                return_value={
                    "org/two/invoice.pdf": (changed_key, 10),
                    "org/new/invoice.pdf": (new_key, 20),
                },
            ) as changed,
            mock.patch.object(
                backup_storage, "upload_manifest", side_effect=capture_manifest
            ),
        ):
            summary = backup_storage.execute(config)

        self.assertEqual(changed.call_count, 1)
        self.assertEqual(
            summary["documents"],
            {
                "scanned": 3,
                "reused": 1,
                "uploaded": 2,
                "uploaded_bytes": 30,
                "deleted_since_previous": 1,
            },
        )
        entries = uploaded_manifest["buckets"]["documents"]["objects"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            {entry["backup_key"] for entry in entries},
            {unchanged_key, changed_key, new_key},
        )

    def test_public_error_output_never_contains_customer_path(self) -> None:
        sensitive_path = "org/customer-name/private-import.xlsx"
        stderr = io.StringIO()

        with (
            mock.patch.object(
                backup_storage.Settings, "from_environment", return_value=settings()
            ),
            mock.patch.object(
                backup_storage, "execute", side_effect=RuntimeError(sensitive_path)
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = backup_storage.main()

        self.assertEqual(result, 1)
        self.assertNotIn(sensitive_path, stderr.getvalue())
        self.assertIn("Reference:", stderr.getvalue())

    def test_settings_rejects_untrusted_bucket_names(self) -> None:
        environment = {
            "R2_BUCKET": "private-bucket",
            "RUNNER_TEMP": "/tmp",
            "STORAGE_BUCKETS": "documents,$(unsafe)",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(backup_storage.BackupError):
                backup_storage.Settings.from_environment()

    def test_source_paths_cannot_escape_the_private_temp_directory(self) -> None:
        for unsafe_path in (
            "../customer.pdf",
            "/absolute/customer.pdf",
            "org//customer.pdf",
            "org/./customer.pdf",
            "org/customer\nname.pdf",
        ):
            with self.subTest(path=unsafe_path):
                with self.assertRaises(backup_storage.BackupError):
                    backup_storage.validate_source_item(
                        source_item(unsafe_path, "hash")
                    )


if __name__ == "__main__":
    unittest.main()

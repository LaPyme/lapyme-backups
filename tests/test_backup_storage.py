from __future__ import annotations

import contextlib
import copy
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
        batch_size=2,
        transfers=32,
        checkers=64,
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

    def test_execute_checkpoints_every_completed_batch(self) -> None:
        config = settings()
        source = [
            source_item(f"org/private-{index}.pdf", f"md5-{index}")
            for index in range(5)
        ]
        keys = {
            item["Path"]: backup_key("documents", seed)
            for item, seed in zip(source, ("ab", "cd", "ef", "01", "23"))
        }
        checkpoints: list[dict[str, object]] = []

        def back_up_batch(
            _settings: backup_storage.Settings,
            _bucket: str,
            batch: list[dict[str, object]],
            _destination_objects: set[str],
        ) -> dict[str, tuple[str, int]]:
            return {item["Path"]: (keys[item["Path"]], 10) for item in batch}

        def capture_checkpoint(
            _settings: backup_storage.Settings, manifest: dict[str, object]
        ) -> str:
            checkpoints.append(copy.deepcopy(manifest))
            return "digest"

        with (
            mock.patch.object(backup_storage, "load_latest_manifest", return_value=None),
            mock.patch.object(
                backup_storage, "list_destination_objects", return_value=set()
            ),
            mock.patch.object(
                backup_storage,
                "run_rclone",
                return_value=json.dumps(source).encode("utf-8"),
            ),
            mock.patch.object(
                backup_storage,
                "backup_changed_objects",
                side_effect=back_up_batch,
            ) as changed,
            mock.patch.object(
                backup_storage, "upload_manifest", side_effect=capture_checkpoint
            ),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            backup_storage.execute(config)

        self.assertEqual(
            [len(call.args[2]) for call in changed.call_args_list],
            [2, 2, 1],
        )
        self.assertEqual(
            [checkpoint["complete"] for checkpoint in checkpoints],
            [False, False, False, True],
        )
        self.assertEqual(
            [
                len(checkpoint["buckets"]["documents"]["objects"])
                for checkpoint in checkpoints
            ],
            [2, 4, 5, 5],
        )
        self.assertIn("documents 100%", stdout.getvalue())
        for item in source:
            self.assertNotIn(item["Path"], stdout.getvalue())

    def test_interrupted_run_resumes_from_last_checkpoint(self) -> None:
        config = settings()
        source = [
            source_item(f"org/private-{index}.pdf", f"md5-{index}")
            for index in range(4)
        ]
        keys = {
            item["Path"]: backup_key("documents", seed)
            for item, seed in zip(source, ("ab", "cd", "ef", "01"))
        }
        checkpoints: list[dict[str, object]] = []
        first_run_batches = 0

        def interrupt_second_batch(
            _settings: backup_storage.Settings,
            _bucket: str,
            batch: list[dict[str, object]],
            _destination_objects: set[str],
        ) -> dict[str, tuple[str, int]]:
            nonlocal first_run_batches
            first_run_batches += 1
            if first_run_batches == 2:
                raise backup_storage.BackupError("interrupted")
            return {item["Path"]: (keys[item["Path"]], 10) for item in batch}

        def capture_checkpoint(
            _settings: backup_storage.Settings, manifest: dict[str, object]
        ) -> str:
            checkpoints.append(copy.deepcopy(manifest))
            return "digest"

        with (
            mock.patch.object(backup_storage, "load_latest_manifest", return_value=None),
            mock.patch.object(
                backup_storage, "list_destination_objects", return_value=set()
            ),
            mock.patch.object(
                backup_storage,
                "run_rclone",
                return_value=json.dumps(source).encode("utf-8"),
            ),
            mock.patch.object(
                backup_storage,
                "backup_changed_objects",
                side_effect=interrupt_second_batch,
            ),
            mock.patch.object(
                backup_storage, "upload_manifest", side_effect=capture_checkpoint
            ),
        ):
            with self.assertRaises(backup_storage.BackupError):
                backup_storage.execute(config)

        self.assertEqual(len(checkpoints), 1)
        self.assertFalse(checkpoints[0]["complete"])
        checkpoint_keys = {
            entry["backup_key"]
            for entry in checkpoints[0]["buckets"]["documents"]["objects"]
        }
        self.assertEqual(
            checkpoint_keys,
            {keys[source[0]["Path"]], keys[source[1]["Path"]]},
        )

        resumed_batches: list[list[str]] = []

        def back_up_remaining(
            _settings: backup_storage.Settings,
            _bucket: str,
            batch: list[dict[str, object]],
            _destination_objects: set[str],
        ) -> dict[str, tuple[str, int]]:
            resumed_batches.append([item["Path"] for item in batch])
            return {item["Path"]: (keys[item["Path"]], 10) for item in batch}

        with (
            mock.patch.object(
                backup_storage,
                "load_latest_manifest",
                return_value=checkpoints[0],
            ),
            mock.patch.object(
                backup_storage,
                "list_destination_objects",
                return_value=set(checkpoint_keys),
            ),
            mock.patch.object(
                backup_storage,
                "run_rclone",
                return_value=json.dumps(source).encode("utf-8"),
            ),
            mock.patch.object(
                backup_storage,
                "backup_changed_objects",
                side_effect=back_up_remaining,
            ),
            mock.patch.object(backup_storage, "upload_manifest", return_value="digest"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            summary = backup_storage.execute(config)

        self.assertEqual(
            resumed_batches,
            [[source[2]["Path"], source[3]["Path"]]],
        )
        self.assertEqual(summary["documents"]["reused"], 2)
        self.assertEqual(summary["documents"]["uploaded"], 2)

    def test_checkpoint_manifests_use_unique_immutable_keys(self) -> None:
        config = settings()
        manifest = {
            "version": 1,
            "created_at": "20260720T120000000000Z",
            "complete": False,
            "buckets": {},
        }
        first = backup_storage.datetime(
            2026,
            7,
            20,
            12,
            0,
            0,
            1,
            tzinfo=backup_storage.timezone.utc,
        )
        second = first.replace(microsecond=2)

        with mock.patch.object(backup_storage, "datetime") as current_datetime:
            current_datetime.now.side_effect = (first, second)
            with mock.patch.object(
                backup_storage, "run_rclone", return_value=b""
            ) as run:
                backup_storage.upload_manifest(config, manifest)
                backup_storage.upload_manifest(config, manifest)

        first_arguments = run.call_args_list[0].args[1]
        second_arguments = run.call_args_list[1].args[1]
        self.assertIn("--immutable", first_arguments)
        self.assertNotEqual(first_arguments[-1], second_arguments[-1])
        self.assertIn("storage-manifest_20260720T120000000001Z_", first_arguments[-1])
        self.assertIn("storage-manifest_20260720T120000000002Z_", second_arguments[-1])

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

    def test_settings_rejects_invalid_parallelism(self) -> None:
        environment = {
            "R2_BUCKET": "private-bucket",
            "RUNNER_TEMP": "/tmp",
            "STORAGE_BATCH_SIZE": "0",
            "STORAGE_TRANSFERS": "32",
            "STORAGE_CHECKERS": "64",
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

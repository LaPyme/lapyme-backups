#!/usr/bin/env python3
"""Create a privacy-safe, incremental backup of Supabase Storage in R2.

Object paths can contain customer identifiers and original filenames. They are
kept only inside an R2-hosted manifest; public workflow logs and R2 object keys
contain counts and cryptographic hashes only.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
MANIFEST_PATTERN = re.compile(
    r"^storage-manifest_(\d{8}T\d{6}(?:\d{6})?Z)_([0-9a-f]{64})\.json\.gz$"
)


class BackupError(RuntimeError):
    """An intentionally redacted backup failure."""


@dataclass(frozen=True)
class Settings:
    rclone: str
    source_remote: str
    destination_remote: str
    destination_bucket: str
    destination_prefix: str
    buckets: tuple[str, ...]
    temp_dir: Path

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "R2_BUCKET": os.environ.get("R2_BUCKET", ""),
            "RUNNER_TEMP": os.environ.get("RUNNER_TEMP", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise BackupError("Required backup configuration is missing")

        buckets = tuple(
            bucket.strip()
            for bucket in os.environ.get(
                "STORAGE_BUCKETS", "documents,imports,public_assets"
            ).split(",")
            if bucket.strip()
        )
        if not buckets or any(not re.fullmatch(r"[a-z0-9_-]+", item) for item in buckets):
            raise BackupError("Storage bucket configuration is invalid")

        prefix = os.environ.get("R2_STORAGE_PREFIX", "storage/v1").strip("/")
        if not prefix or not re.fullmatch(r"[a-zA-Z0-9/_-]+", prefix):
            raise BackupError("R2 storage prefix is invalid")

        return cls(
            rclone=os.environ.get("RCLONE_BIN", "rclone"),
            source_remote=os.environ.get("SOURCE_REMOTE", "supabase"),
            destination_remote=os.environ.get("DESTINATION_REMOTE", "r2"),
            destination_bucket=required["R2_BUCKET"],
            destination_prefix=prefix,
            buckets=buckets,
            temp_dir=Path(required["RUNNER_TEMP"]),
        )

    def source(self, bucket: str, path: str = "") -> str:
        suffix = f"/{path}" if path else ""
        return f"{self.source_remote}:{bucket}{suffix}"

    def destination(self, path: str = "") -> str:
        suffix = f"/{path}" if path else ""
        return f"{self.destination_remote}:{self.destination_bucket}{suffix}"


def error_reference(error: BaseException) -> str:
    del error
    return secrets.token_hex(6)


def run_rclone(
    settings: Settings,
    arguments: list[str],
    *,
    allow_missing: bool = False,
) -> bytes:
    result = subprocess.run(
        [settings.rclone, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if allow_missing and result.returncode == 3:
        return b""

    reference = hashlib.sha256(result.stderr).hexdigest()[:12]
    raise BackupError(f"rclone operation failed with reference {reference}")


def safe_json_list(raw: bytes) -> list[dict[str, Any]]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise BackupError("Unexpected object inventory format")
    return value


def source_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    hashes = item.get("Hashes")
    md5 = hashes.get("MD5", "") if isinstance(hashes, dict) else ""
    size = item.get("Size")
    mod_time = item.get("ModTime")
    if not isinstance(size, int) or size < 0 or not isinstance(mod_time, str):
        raise BackupError("Unexpected object metadata")
    if isinstance(md5, str) and md5:
        return ("md5", md5.lower(), size)
    return ("size-modtime", size, mod_time)


def manifest_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    md5 = item.get("source_md5")
    size = item.get("size")
    mod_time = item.get("mod_time")
    if isinstance(md5, str) and md5:
        return ("md5", md5.lower(), size)
    return ("size-modtime", size, mod_time)


def is_safe_source_path(path: Any) -> bool:
    return bool(
        isinstance(path, str)
        and path
        and "\0" not in path
        and "\n" not in path
        and "\r" not in path
        and not path.startswith("/")
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def validate_source_item(item: dict[str, Any]) -> str:
    path = item.get("Path")
    if not is_safe_source_path(path):
        raise BackupError("Unexpected object path")
    if not isinstance(path, str):
        # Narrow the type for static analyzers; is_safe_source_path already
        # rejects every non-string value.
        raise BackupError("Unexpected object path")
    source_signature(item)
    return path


def is_valid_backup_key(settings: Settings, bucket: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    prefix = re.escape(f"{settings.destination_prefix}/objects/{bucket}/")
    return bool(
        re.fullmatch(
            rf"{prefix}[0-9a-f]{{2}}/[0-9a-f]{{64}}/[0-9a-f]{{64}}", value
        )
    )


def list_destination_objects(settings: Settings) -> set[str]:
    root = f"{settings.destination_prefix}/objects"
    raw = run_rclone(
        settings,
        ["lsf", "--recursive", "--files-only", settings.destination(root)],
        allow_missing=True,
    )
    return {
        f"{root}/{line}"
        for line in raw.decode("utf-8").splitlines()
        if line
    }


def load_latest_manifest(settings: Settings) -> dict[str, Any] | None:
    manifest_root = f"{settings.destination_prefix}/manifests"
    raw = run_rclone(
        settings,
        ["lsjson", "--files-only", settings.destination(manifest_root)],
        allow_missing=True,
    )
    listed = safe_json_list(raw) if raw else []
    candidates = sorted(
        (
            item["ModTime"],
            item["Name"],
        )
        for item in listed
        if isinstance(item.get("Name"), str)
        and isinstance(item.get("ModTime"), str)
        and MANIFEST_PATTERN.fullmatch(item["Name"])
    )
    if not candidates:
        return None

    _, name = candidates[-1]
    match = MANIFEST_PATTERN.fullmatch(name)
    if match is None:
        raise BackupError("Manifest name validation failed")

    with tempfile.NamedTemporaryFile(
        prefix="storage-manifest-",
        suffix=".json.gz",
        dir=settings.temp_dir,
        delete=False,
    ) as temporary:
        local_path = Path(temporary.name)

    try:
        run_rclone(
            settings,
            ["copyto", settings.destination(f"{manifest_root}/{name}"), str(local_path)],
        )
        compressed = local_path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != match.group(2):
            raise BackupError("Manifest checksum validation failed")
        manifest = json.loads(gzip.decompress(compressed).decode("utf-8"))
    finally:
        local_path.unlink(missing_ok=True)

    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise BackupError("Unsupported storage manifest")
    return manifest


def previous_bucket_map(
    settings: Settings,
    manifest: dict[str, Any] | None,
    bucket: str,
) -> dict[str, dict[str, Any]]:
    if manifest is None:
        return {}
    buckets = manifest.get("buckets")
    if not isinstance(buckets, dict):
        raise BackupError("Invalid storage manifest buckets")
    bucket_value = buckets.get(bucket, {})
    if not isinstance(bucket_value, dict):
        raise BackupError("Invalid storage manifest bucket")
    objects = bucket_value.get("objects", [])
    if not isinstance(objects, list):
        raise BackupError("Invalid storage manifest objects")

    result: dict[str, dict[str, Any]] = {}
    for item in objects:
        if not isinstance(item, dict):
            raise BackupError("Invalid storage manifest object")
        path = item.get("path")
        key = item.get("backup_key")
        if (
            not is_safe_source_path(path)
            or not is_valid_backup_key(settings, bucket, key)
        ):
            raise BackupError("Invalid storage manifest entry")
        result[path] = item
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_changed_objects(
    settings: Settings,
    bucket: str,
    sources: list[dict[str, Any]],
    destination_objects: set[str],
) -> dict[str, tuple[str, int]]:
    if not sources:
        return {}

    work_root = Path(
        tempfile.mkdtemp(
            prefix=f"storage-{bucket}-",
            dir=settings.temp_dir,
        )
    )
    source_root = work_root / "source"
    staging_root = work_root / "staging"
    files_from = work_root / "files-from-raw.txt"
    source_root.mkdir()
    staging_root.mkdir()

    try:
        source_paths = [validate_source_item(source) for source in sources]
        files_from.write_text("\n".join(source_paths) + "\n", encoding="utf-8")

        # One rclone process downloads all changed objects concurrently. Raw
        # customer paths stay in the private runner temp directory and captured
        # subprocess output only.
        run_rclone(
            settings,
            [
                "copy",
                "--quiet",
                "--files-from-raw",
                str(files_from),
                "--transfers",
                "8",
                "--checkers",
                "16",
                settings.source(bucket),
                str(source_root),
            ],
        )

        results: dict[str, tuple[str, int]] = {}
        new_keys: set[str] = set()
        for source, source_path in zip(sources, source_paths):
            local_path = source_root.joinpath(*source_path.split("/"))
            if not local_path.is_file() or local_path.stat().st_size != source["Size"]:
                raise BackupError("Downloaded object failed local validation")

            content_hash = file_sha256(local_path)
            path_hash = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
            backup_key = (
                f"{settings.destination_prefix}/objects/{bucket}/"
                f"{path_hash[:2]}/{path_hash}/{content_hash}"
            )
            uploaded_bytes = 0
            if backup_key not in destination_objects and backup_key not in new_keys:
                relative_key = backup_key.removeprefix(
                    f"{settings.destination_prefix}/objects/"
                )
                staged_path = staging_root.joinpath(*relative_key.split("/"))
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                os.link(local_path, staged_path)
                new_keys.add(backup_key)
                uploaded_bytes = local_path.stat().st_size
            results[source_path] = (backup_key, uploaded_bytes)

        if new_keys:
            run_rclone(
                settings,
                [
                    "copy",
                    "--quiet",
                    "--immutable",
                    "--transfers",
                    "8",
                    "--checkers",
                    "16",
                    str(staging_root),
                    settings.destination(f"{settings.destination_prefix}/objects"),
                ],
            )
            destination_objects.update(new_keys)
        return results
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def create_manifest_entry(source: dict[str, Any], backup_key: str) -> dict[str, Any]:
    hashes = source.get("Hashes")
    md5 = hashes.get("MD5", "") if isinstance(hashes, dict) else ""
    mime_type = source.get("MimeType")
    return {
        "path": source["Path"],
        "size": source["Size"],
        "mod_time": source["ModTime"],
        "source_md5": md5 if isinstance(md5, str) else "",
        "mime_type": mime_type if isinstance(mime_type, str) else "",
        "backup_key": backup_key,
    }


def upload_manifest(settings: Settings, manifest: dict[str, Any]) -> str:
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    compressed = gzip.compress(serialized, compresslevel=9, mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()
    timestamp = manifest["created_at"].replace("-", "").replace(":", "")
    name = f"storage-manifest_{timestamp}_{digest}.json.gz"

    with tempfile.NamedTemporaryFile(
        prefix="storage-manifest-",
        suffix=".json.gz",
        dir=settings.temp_dir,
        delete=False,
    ) as temporary:
        temporary.write(compressed)
        local_path = Path(temporary.name)

    try:
        remote_path = f"{settings.destination_prefix}/manifests/{name}"
        run_rclone(
            settings,
            ["copyto", "--quiet", "--no-traverse", str(local_path), settings.destination(remote_path)],
        )
    finally:
        local_path.unlink(missing_ok=True)
    return digest


def execute(settings: Settings) -> dict[str, dict[str, int]]:
    previous_manifest = load_latest_manifest(settings)
    destination_objects = list_destination_objects(settings)
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
        "buckets": {},
    }
    summaries: dict[str, dict[str, int]] = {}

    for bucket in settings.buckets:
        raw = run_rclone(
            settings,
            ["lsjson", "--recursive", "--files-only", "--hash", settings.source(bucket)],
        )
        source_objects = safe_json_list(raw)
        previous = previous_bucket_map(settings, previous_manifest, bucket)
        current_paths: set[str] = set()
        entries: list[dict[str, Any]] = []
        changed_sources: list[dict[str, Any]] = []
        reused = 0
        uploaded = 0
        uploaded_bytes = 0

        for source in source_objects:
            source_path = validate_source_item(source)
            if source_path in current_paths:
                raise BackupError("Duplicate object path in source inventory")
            current_paths.add(source_path)

            prior = previous.get(source_path)
            prior_key = prior.get("backup_key") if prior else None
            if (
                prior is not None
                and source_signature(source) == manifest_signature(prior)
                and is_valid_backup_key(settings, bucket, prior_key)
                and prior_key in destination_objects
            ):
                backup_key = prior_key
                reused += 1
                entries.append(create_manifest_entry(source, backup_key))
            else:
                changed_sources.append(source)

        changed_results = backup_changed_objects(
            settings,
            bucket,
            changed_sources,
            destination_objects,
        )
        for source in changed_sources:
            source_path = source["Path"]
            backup_key, copied_bytes = changed_results[source_path]
            uploaded += 1 if copied_bytes else 0
            uploaded_bytes += copied_bytes
            entries.append(create_manifest_entry(source, backup_key))

        entries.sort(key=lambda item: item["path"])
        manifest["buckets"][bucket] = {"objects": entries}
        summaries[bucket] = {
            "scanned": len(entries),
            "reused": reused,
            "uploaded": uploaded,
            "uploaded_bytes": uploaded_bytes,
            "deleted_since_previous": len(set(previous) - current_paths),
        }

    upload_manifest(settings, manifest)
    return summaries


def main() -> int:
    try:
        settings = Settings.from_environment()
        summaries = execute(settings)
    except Exception as error:  # The public log must never include object paths.
        print(
            "::error::Storage backup failed; customer object details were suppressed. "
            f"Reference: {error_reference(error)}",
            file=sys.stderr,
        )
        return 1

    print("Storage backup completed; production inventory details were kept private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

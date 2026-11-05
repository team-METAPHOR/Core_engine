"""Private, bounded per-job JSON/PNG storage for a trusted backend worker."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .images import Screenshot
from .model import strict_json


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _directory(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid job ID")
        directory = self.root / job_id
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise ValueError("invalid job directory")
        return directory

    def create(self) -> str:
        job_id = uuid4().hex
        self._directory(job_id).mkdir(mode=0o700)
        return job_id

    def _path(self, job_id, name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.(json|png|md|webm|spec\.ts)", name):
            raise ValueError("invalid artifact name")
        directory = self._directory(job_id)
        if not directory.is_dir():
            raise ValueError("unknown job")
        path = directory / name
        if path.is_symlink():
            raise ValueError("artifact symlinks are forbidden")
        return path

    def write(self, job_id: str, name: str, data: bytes):
        if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
            raise ValueError("artifact exceeds 8 MiB")
        path = self._path(job_id, name)
        temporary = path.with_name(uuid4().hex + ".tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, job_id: str, name: str) -> bytes:
        path = self._path(job_id, name)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("artifact exceeds 8 MiB")
        return data

    def write_json(self, job_id: str, name: str, value):
        self.write(job_id, name, json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode())

    def read_json(self, job_id: str, name: str):
        return strict_json(self.read(job_id, name).decode("utf-8"))

    def image(self, job_id: str, screenshot: Screenshot) -> dict:
        name = "image_" + screenshot.sha256 + ".png"
        self.write(job_id, name, screenshot.data)
        return {"name": name, "sha256": screenshot.sha256, "bytes": len(screenshot.data)}

    def read_image(self, job_id: str, reference: dict) -> Screenshot:
        data = self.read(job_id, reference["name"])
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("artifact digest mismatch")
        return Screenshot(data)

    def event(self, job_id: str, kind: str, detail: dict | None = None):
        # One worker owns a job. Atomic snapshots are readable during execution.
        try:
            events = self.read_json(job_id, "events.json")
        except FileNotFoundError:
            events = []
        if len(events) >= 200:
            raise ValueError("event limit exceeded")
        events.append({"sequence": len(events) + 1, "kind": kind,
                       "created_at": datetime.now(timezone.utc).isoformat(), "detail": detail or {}})
        self.write_json(job_id, "events.json", events)

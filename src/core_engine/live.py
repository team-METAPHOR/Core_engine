"""Opt-in, bounded live observation for trusted worker adapters (no network server)."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
import asyncio
import json
import re
import threading
import time

from PIL import Image


@dataclass(frozen=True)
class LiveOptions:
    fps: int = 2
    max_width: int = 960
    jpeg_quality: int = 65
    max_frame_bytes: int = 256 * 1024
    event_capacity: int = 128

    def __post_init__(self):
        for name, low, high in (("fps", 1, 4), ("max_width", 320, 1280),
                                ("jpeg_quality", 30, 85), ("max_frame_bytes", 16 * 1024, 512 * 1024),
                                ("event_capacity", 8, 256)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid live preview option: " + name)


@dataclass(frozen=True)
class LiveMessage:
    """Metadata is copied on read; image bytes are immutable and excluded from repr."""
    sequence: int
    kind: str
    created_at: str
    job_id: str
    program_id: str
    report_id: str
    _data_json: str = field(repr=False)
    image: bytes | None = field(default=None, repr=False)

    def metadata(self) -> dict:
        return {"schema_version": "live-1", "sequence": self.sequence, "kind": self.kind,
                "created_at": self.created_at, "job_id": self.job_id, "program_id": self.program_id,
                "report_id": self.report_id, "data": json.loads(self._data_json)}

    def to_wire(self) -> str | bytes:
        """WebSocket text event, or [4-byte big-endian JSON length][JSON][JPEG]."""
        text = json.dumps(self.metadata(), ensure_ascii=True, separators=(",", ":"))
        if self.image is None:
            return text
        header = text.encode("utf-8")
        return len(header).to_bytes(4, "big") + header + self.image


class LiveStream:
    """One QA run per instance. read() never waits for a viewer or performs I/O.

    Progress has a bounded replay window; only the newest JPEG is retained. Sequence
    gaps are normal. A backend must authorize readers and bridge process boundaries.
    """
    def __init__(self, options: LiveOptions = LiveOptions()):
        if not isinstance(options, LiveOptions):
            raise ValueError("invalid live options")
        self.options = options
        self._lock = threading.Lock()
        self._identity = None
        self._events = deque(maxlen=options.event_capacity)
        self._latest_frame = None
        self._sequence = 0
        self._closed = False
        self._last_frame_at = float("-inf")
        self._context = {}
        self._warnings = set()

    @property
    def job_id(self):
        with self._lock:
            return self._identity[0] if self._identity else None

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def read(self, after_sequence: int = 0) -> tuple[LiveMessage, ...]:
        """Non-destructive, thread-safe read. Use read(0) to reconnect to current state."""
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("invalid live cursor")
        with self._lock:
            messages = [event for event in self._events if event.sequence > after_sequence]
            if self._latest_frame and self._latest_frame.sequence > after_sequence:
                messages.append(self._latest_frame)
            return tuple(sorted(messages, key=lambda message: message.sequence))

    def _bind(self, job_id, program_id, report_id):
        with self._lock:
            if self._identity is not None or self._closed:
                raise ValueError("use a new LiveStream for every QA run")
            self._identity = (job_id, program_id, report_id)

    def _emit_locked(self, kind, data, image=None):
        if self._closed or self._identity is None:
            return
        self._sequence += 1
        message = LiveMessage(self._sequence, kind, datetime.now(timezone.utc).isoformat(),
                              *self._identity, json.dumps(data, ensure_ascii=True, allow_nan=False), image)
        if image is None:
            self._events.append(message)
        else:
            self._latest_frame = message

    def _status(self, phase, detail=None):
        # Never relay arbitrary job-event details, URLs, selectors, model text or credentials.
        data = {"phase": phase}
        for key in ("revision", "step"):
            value = (detail or {}).get(key)
            if type(value) is int:
                data["step_index" if key == "step" else key] = value
        with self._lock:
            self._emit_locked("status", data)

    def _progress(self, *, attempt, revision, phase, step_index=None, action=None, total_steps=None):
        data = {"attempt": attempt, "revision": revision, "phase": phase,
                "step_index": step_index, "action": action, "total_steps": total_steps}
        with self._lock:
            if phase == "attempt_started":
                # A new browser/context must never display a previous attempt as live.
                self._latest_frame = None
                self._last_frame_at = float("-inf")
            self._context = data
            self._emit_locked("progress", data)

    def _warning(self, code):
        with self._lock:
            if code not in self._warnings:
                self._warnings.add(code)
                self._emit_locked("preview_warning", {"error_code": code})

    def _frame(self, image):
        """Only Recorder/GuardedPage may supply already-masked screenshots here."""
        now = time.monotonic()
        with self._lock:
            if self._closed or self._identity is None or now - self._last_frame_at < 1 / self.options.fps:
                return
            self._last_frame_at = now
            context = dict(self._context)
        try:
            with Image.open(BytesIO(image.data)) as source:
                preview = source.convert("RGB")
                preview.thumbnail((self.options.max_width, self.options.max_width), Image.Resampling.LANCZOS)
                buffer = BytesIO()
                preview.save(buffer, format="JPEG", quality=self.options.jpeg_quality)
                payload = buffer.getvalue()
                width, height = preview.size
            if len(payload) > self.options.max_frame_bytes:
                self._warning("LIVE_FRAME_TOO_LARGE")
                return
            with self._lock:
                self._emit_locked("frame", {**context, "mime_type": "image/jpeg", "width": width,
                                  "height": height, "bytes": len(payload), "masked": True}, payload)
        except Exception:
            # Preview encoding cannot change the browser verdict or leak exception bodies.
            self._warning("LIVE_FRAME_ENCODING_FAILED")

    def _end(self, status, *, error_code=None):
        with self._lock:
            if self._closed:
                return
            if status in {"cancelled", "interrupted"} or error_code in {"AUTHORIZATION_REVOKED", "AUTHORIZATION_UNAVAILABLE", "CANCELLED"}:
                self._latest_frame = None
            code = error_code if isinstance(error_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", error_code) else None
            self._emit_locked("end", {"status": status, "error_code": code})
            self._closed = True


class PreviewCapture:
    """Shared masked frame sampling when live preview is enabled, with optional video."""
    def __init__(self, recorder, session):
        self.recorder, self.session = recorder, session
        self.task = asyncio.create_task(self._sample())

    async def _sample(self):
        try:
            while not self.recorder.live.closed:
                started = time.monotonic()
                video = self.session.video
                fps = self.recorder.live.options.fps
                if video and video.metadata["status"] == "recording" and not video.metadata["truncated"]:
                    fps = max(fps, video.options.video_fps)
                image = await self.session.capture_video_frame()
                if image is not None:
                    if video:
                        video.add(image)
                    self.recorder.live._frame(image)
                await asyncio.sleep(max(0.01, 1 / fps - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.recorder.live._warning("LIVE_CAPTURE_INTERRUPTED")
            if self.session.video:
                self.session.video.metadata.update(capture_error="VIDEO_CAPTURE_INTERRUPTED", truncated=True)

    async def finish(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

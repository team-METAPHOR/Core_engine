"""Bounded WebM recording from continuously sampled, already-masked browser frames."""

import asyncio
import hashlib
import os
import shutil
import time


def find_encoder():
    configured = os.environ.get("FFMPEG_BINARY")
    if configured:
        return configured
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")


class VideoCapture:
    def __init__(self, recorder, capture_frame, *, attempt, origin):
        self.recorder, self.capture_frame, self.origin = recorder, capture_frame, origin
        self.options = recorder.options
        self.encoder = find_encoder()
        self.frames, self.frame_bytes, self.samples = [], 0, 0
        self.metadata = {"attempt": attempt, "revision": recorder.revision, "plan_sha256": recorder.plan_sha256,
            "kind": "masked_capture", "status": "recording", "sampling_fps": self.options.video_fps,
            "has_audio": False, "truncated": False, "capture_error": None, "artifact": None}
        self.task = None

    def start(self):
        if not self.encoder:
            self.metadata.update(status="unavailable", capture_error="VIDEO_ENCODER_UNAVAILABLE")
        else:
            self.task = asyncio.create_task(self._sample())
        return self

    def add(self, image):
        if self.metadata["status"] != "recording" or self.metadata["truncated"]:
            return
        elapsed = time.monotonic() - self.origin
        if elapsed > self.options.video_max_seconds or self.samples >= 600:
            self.metadata["truncated"] = True
            return
        self.samples += 1
        if self.frames and image.data == self.frames[-1][1]:
            return
        if self.frame_bytes + len(image.data) > 16 * 1024 * 1024:
            self.metadata.update(truncated=True, capture_error="VIDEO_FRAME_BUDGET")
            return
        self.frames.append((elapsed, image.data))
        self.frame_bytes += len(image.data)

    async def _sample(self):
        try:
            while not self.metadata["truncated"]:
                started = time.monotonic()
                if started - self.origin >= self.options.video_max_seconds:
                    self.metadata["truncated"] = True
                    break
                image = await self.capture_frame()
                if image is not None:
                    self.add(image)
                await asyncio.sleep(max(0.01, 1 / self.options.video_fps - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.metadata.update(capture_error="VIDEO_CAPTURE_INTERRUPTED", truncated=True)

    async def finish(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.metadata["status"] == "unavailable":
            self.recorder.store.write_json(self.recorder.job_id, "videos.json", self.recorder.videos)
            return
        try:
            if not self.metadata["truncated"]:
                try:
                    image = await self.capture_frame()
                    if image is not None:
                        self.add(image)
                except Exception:
                    self.metadata["capture_error"] = "VIDEO_CAPTURE_INTERRUPTED"
            if not self.frames:
                self.metadata.update(status="empty", capture_error=self.metadata["capture_error"] or "VIDEO_NO_FRAMES")
                return
            start = self.frames[0][0]
            end = min(time.monotonic() - self.origin, self.options.video_max_seconds)
            count = max(1, min(1200, round(max(0, end - start) * self.options.video_fps)))
            data = await self._encode(start, count)
            if self.recorder.total_video_bytes + len(data) > 24 * 1024 * 1024:
                raise ValueError("video job budget")
            digest = hashlib.sha256(data).hexdigest()
            name = "video_" + digest + ".webm"
            self.recorder.store.write(self.recorder.job_id, name, data)
            self.recorder.total_video_bytes += len(data)
            self.metadata.update(status="ready", frames_captured=self.samples,
                start_offset_seconds=round(start, 3), duration_seconds=count / self.options.video_fps,
                artifact={"name": name, "sha256": digest, "bytes": len(data), "mime_type": "video/webm"})
        except asyncio.CancelledError:
            self.metadata.update(status="interrupted", capture_error="VIDEO_FINALIZATION_CANCELLED")
            raise
        except Exception:
            self.metadata.update(status="failed", capture_error="VIDEO_ENCODING_OR_STORAGE_FAILED")
        finally:
            self.frames.clear()
            self.recorder.store.write_json(self.recorder.job_id, "videos.json", self.recorder.videos)

    async def _encode(self, start, count):
        # Frames never touch disk unmasked. No shell, network inputs, audio or generated filters.
        args = [self.encoder, "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "png",
            "-framerate", str(self.options.video_fps), "-i", "pipe:0", "-an", "-c:v", "libvpx-vp9",
            "-b:v", "300k", "-deadline", "realtime", "-vf", "scale=w='min(960,iw)':h=-2",
            "-f", "webm", "pipe:1"]
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def feed():
            cursor = 0
            try:
                for index in range(count):
                    offset = start + index / self.options.video_fps
                    while cursor + 1 < len(self.frames) and self.frames[cursor + 1][0] <= offset:
                        cursor += 1
                    process.stdin.write(self.frames[cursor][1])
                    await process.stdin.drain()
            finally:
                process.stdin.close()

        async def bounded_read(stream, maximum):
            chunks, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise ValueError("encoder output budget")
                chunks.append(chunk)
            return b"".join(chunks)

        tasks = [asyncio.create_task(feed()), asyncio.create_task(bounded_read(process.stdout, 8 * 1024 * 1024)),
                 asyncio.create_task(bounded_read(process.stderr, 64 * 1024))]
        try:
            async with asyncio.timeout(20):
                _, data, _ = await asyncio.gather(*tasks)
                if await process.wait() != 0 or not data.startswith(b"\x1a\x45\xdf\xa3"):
                    raise ValueError("invalid WebM")
                return data
        finally:
            for task in tasks:
                task.cancel()
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(*tasks, return_exceptions=True)

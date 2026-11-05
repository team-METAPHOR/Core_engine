import asyncio
from dataclasses import replace
from io import BytesIO
import json
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from core_engine import BrowserOptions, BrowserPlan, BrowserRunner, Check, JobStore, Recorder, Step
from core_engine.video import find_encoder
from fixture_site import site
from test_transport import local_policy


async def reset(_):
    return True


@unittest.skipUnless(os.environ.get('FFMPEG_BINARY') or shutil.which('ffmpeg') or importlib.util.find_spec('imageio_ffmpeg'),
                     'install .[video] or ffmpeg for real WebM verification')
class VideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(temporary.name)

    async def test_real_video_masks_sensitive_frames_and_preserves_failed_attempts(self):
        with site() as (origin, _, _):
            opts = BrowserOptions(record_video=True, video_fps=4, step_timeout_ms=1000)
            recorder = Recorder(self.store, self.store.create(), opts)
            scenario = BrowserPlan((Step('navigate', origin + '/app/privacy'), Step('click', '#missing')),
                ready=Check('visible', '#search', 'true'), expected=Check('count', '#search', '1'),
                symptom=Check('count', '#search', '2'), step_timeout_ms=300)
            result = await BrowserRunner(local_policy(origin), options=opts, recorder=recorder).run(
                scenario, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertEqual(len(recorder.videos), 2)
            for clip in recorder.videos:
                self.assertEqual(clip['status'], 'ready', clip)
                data = self.store.read(recorder.job_id, clip['artifact']['name'])
                self.assertTrue(data.startswith(b'\x1a\x45\xdf\xa3'))
                self.assertGreater(clip['duration_seconds'], 0)
                self.assertFalse(clip['has_audio'])
                file = self.store.root / recorder.job_id / clip['artifact']['name']
                process = await asyncio.create_subprocess_exec(find_encoder(), '-v', 'error', '-i', str(file),
                    '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                png, error = await process.communicate()
                self.assertEqual(process.returncode, 0, error)
                image = Image.open(BytesIO(png)).convert('RGB')
                # Mask color survives the actual WebM encoder/decoder, across private text + inputs.
                masked = sum(r > 180 and g < 80 and b > 180 for r, g, b in image.getdata())
                self.assertGreater(masked, 500)
            self.assertNotIn('customer-canary', json.dumps(recorder.snapshots))
            self.assertIsNone(recorder.active_video)

    async def test_missing_or_broken_encoder_does_not_change_browser_verdict(self):
        with site() as (origin, _, _):
            scenario = BrowserPlan((Step('navigate', origin + '/app/bug'),),
                ready=Check('text', '#ready', 'ready'), expected=Check('text', '#result', 'empty'),
                symptom=Check('text', '#result', 'cat'))
            for encoder, expected_status in (('', 'unavailable'), ('/nonexistent/ffmpeg', 'failed')):
                opts = BrowserOptions(record_video=True)
                recorder = Recorder(self.store, self.store.create(), opts)
                with patch('core_engine.video.find_encoder', return_value=encoder or None):
                    result = await BrowserRunner(local_policy(origin), options=opts, recorder=recorder).run(
                        scenario, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'not_reproduced')
                self.assertEqual([v['status'] for v in recorder.videos], [expected_status] * 2)
                self.assertEqual(len(self.store.read_json(recorder.job_id, 'videos.json')), 2)

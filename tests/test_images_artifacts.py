from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image, PngImagePlugin

from core_engine import JobStore, Screenshot
from core_engine.reporting import escape


def png(size=(40, 30), **kwargs):
    buffer = BytesIO()
    Image.new('RGB', size, 'white').save(buffer, format='PNG', **kwargs)
    return buffer.getvalue()


class ImageTests(unittest.TestCase):
    def test_metadata_stripped_and_mask_is_applied(self):
        info = PngImagePlugin.PngInfo()
        info.add_text('customer', 'private-canary')
        screenshot = Screenshot(png(pnginfo=info))
        self.assertNotIn(b'private-canary', screenshot.data)
        with Image.open(BytesIO(screenshot.data)) as image:
            self.assertEqual(image.info, {})
        masked = screenshot.masked(((0, 0, 10, 10),))
        with Image.open(BytesIO(masked.data)) as image:
            self.assertEqual(image.getpixel((5, 5)), (0, 0, 0))
            self.assertEqual(image.getpixel((20, 20)), (255, 255, 255))
        self.assertNotIn('base64', repr(screenshot))
        self.assertTrue(screenshot.data_url().startswith('data:image/png;base64,'))

    def test_invalid_types_sizes_formats_and_masks_rejected(self):
        for value in (b'', b'<svg/>', b'not an image', b'x' * (4 * 1024 * 1024 + 1), png((4097, 1)), png((3000, 3000))):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                Screenshot(value)
        animated = BytesIO()
        Image.new('RGB', (10, 10), 'white').save(animated, format='GIF')
        with self.assertRaises(ValueError):
            Screenshot(animated.getvalue())
        for box in ((-1, 0, 10, 10), (0, 0, 100, 100), (0, 0, True, 1)):
            with self.assertRaises(ValueError):
                Screenshot(png()).masked((box,))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / 'jobs')
        self.job = self.store.create()

    def test_private_artifacts_digest_and_event_order(self):
        ref = self.store.image(self.job, Screenshot(png()))
        self.assertEqual(self.store.read_image(self.job, ref).sha256, ref['sha256'])
        path = self.store.root / self.job / ref['name']
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.store.event(self.job, 'received')
        self.store.event(self.job, 'completed')
        events = self.store.read_json(self.job, 'events.json')
        self.assertEqual([e['sequence'] for e in events], [1, 2])
        self.assertEqual([e['kind'] for e in events], ['received', 'completed'])
        self.store.write(self.job, ref['name'], png((10, 10)))
        with self.assertRaises(ValueError):
            self.store.read_image(self.job, ref)

    def test_path_traversal_and_symlinks_rejected(self):
        for job, name in (('../escape', 'x.json'), (self.job, '../escape.json'), (self.job, '/tmp/x.json')):
            with self.assertRaises(ValueError):
                self.store.write(job, name, b'{}')
        outside = Path(self.temp.name) / 'outside.json'
        outside.write_text('private')
        (self.store.root / self.job / 'linked.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.read(self.job, 'linked.json')
        with self.assertRaises(ValueError):
            self.store.write(self.job, 'linked.json', b'{}')
        another = 'a' * 32
        (self.store.root / another).symlink_to(Path(self.temp.name))
        with self.assertRaises(ValueError):
            self.store.read(another, 'outside.json')
        self.assertEqual(outside.read_text(), 'private')

    def test_report_text_does_not_become_html_or_links(self):
        text = escape('<script>alert(1)</script> [bad](https://example.com) ![tracking](https://example.com/a)')
        self.assertNotIn('<script>', text)
        self.assertNotIn('[bad](', text)
        self.assertNotIn('![tracking](', text)


if __name__ == '__main__':
    unittest.main()

"""Bounded local screenshot ingestion. Never fetch model-supplied image URLs."""

import base64
from dataclasses import dataclass, field
from io import BytesIO
import hashlib
import warnings

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class Screenshot:
    data: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= 4 * 1024 * 1024:
            raise ValueError("screenshot must contain at most 4 MiB")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(self.data)) as source:
                    if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                        raise ValueError("only static PNG, JPEG, WEBP screenshots are supported")
                    width, height = source.size
                    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 8_000_000:
                        raise ValueError("screenshot dimensions exceed limits")
                    source.load()
                    source = ImageOps.exif_transpose(source)
                    # A new raster removes EXIF, text chunks, ICC profiles and other metadata.
                    clean = Image.new("RGB", source.size, "white")
                    rgba = source.convert("RGBA")
                    clean.paste(rgba, mask=rgba.getchannel("A"))
                    output = BytesIO()
                    clean.save(output, format="PNG")
                    if output.tell() > 4 * 1024 * 1024:
                        raise ValueError("normalized screenshot exceeds 4 MiB")
                    object.__setattr__(self, "data", output.getvalue())
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError("invalid screenshot") from None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.data).decode("ascii")

    def masked(self, boxes: tuple[tuple[int, int, int, int], ...]) -> "Screenshot":
        with Image.open(BytesIO(self.data)) as source:
            clean = source.copy()
        draw = ImageDraw.Draw(clean)
        for box in boxes:
            if len(box) != 4 or any(type(v) is not int for v in box):
                raise ValueError("mask boxes require four integer coordinates")
            x1, y1, x2, y2 = box
            if not 0 <= x1 < x2 <= clean.width or not 0 <= y1 < y2 <= clean.height:
                raise ValueError("mask box is outside the screenshot")
            draw.rectangle(box, fill="black")
        output = BytesIO()
        clean.save(output, format="PNG")
        return Screenshot(output.getvalue())

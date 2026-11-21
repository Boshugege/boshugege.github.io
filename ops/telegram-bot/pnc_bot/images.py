"""Image pipeline: decode untrusted bytes, re-encode as a clean WebP.

Every image is decoded and re-encoded from raw pixels, so nothing from the
original file survives except the picture itself: no EXIF (GPS, camera,
orientation), XMP, ICC profile or comments. ``assert_clean_webp`` checks the
output at the RIFF chunk level before anything is written to the repo.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

from PIL import Image, ImageCms, ImageOps

__all__ = [
    "ImageRejected",
    "ProcessedImage",
    "MAX_INPUT_BYTES",
    "MAX_PIXELS",
    "MAX_WIDTH",
    "IMAGE_NAME_RE",
    "HEIC_MESSAGE",
    "process_image",
    "assert_clean_webp",
    "image_filename",
]

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_WIDTH = 1600
WEBP_MAX_DIMENSION = 16383  # hard limit of the WebP format
IMAGE_NAME_RE = r"^\d{8}-[0-9a-f]{10}\.webp$"

HEIC_MESSAGE = "服务器不支持 HEIC，请用“照片”方式发送（会自动转成 JPEG）"
_TOO_LARGE = "图片太大了（超过 20 MB）"
_TOO_MANY_PIXELS = "图片像素太多（超过 5000 万像素）"
_UNREADABLE = "无法识别这张图片（支持 JPEG、PNG、WebP、GIF、BMP、TIFF）"
_ANIMATED = "不支持动图或多页图片，请发静态图片"

# Only these decoders ever see the bytes. Pillow's JPEG opener returns format
# "MPO" for a JPEG with extra embedded images (common from phone cameras);
# its first frame is used.
_DECODERS = ("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF")
_FORMATS = frozenset(_DECODERS) | {"MPO"}
_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1", b"mif2"}
)
_AVIF_BRANDS = frozenset({b"avif", b"avis"})

_WEBP_ALLOWED_CHUNKS = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH"})
_VP8X_ICC, _VP8X_EXIF, _VP8X_XMP, _VP8X_ANIMATION = 0x20, 0x08, 0x04, 0x02

_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


class ImageRejected(Exception):
    """The image cannot be used; the message is a short sentence for the user."""


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    width: int
    height: int
    sha256: str


def process_image(data: bytes, *, max_width: int = MAX_WIDTH, quality: int = 82) -> ProcessedImage:
    """Decode ``data`` and return it as a metadata-free WebP no wider than ``max_width``.

    Raises ImageRejected for anything the bot should refuse (size, format,
    animation, undecodable data).
    """
    if max_width < 1:
        raise ValueError("max_width must be positive")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be 1..100")
    data = bytes(data)
    if len(data) > MAX_INPUT_BYTES:
        raise ImageRejected(_TOO_LARGE)
    if _looks_like_heif(data):
        raise ImageRejected(HEIC_MESSAGE)

    image = _open(data, max_width)
    icc_profile = image.info.get("icc_profile")
    image = _exif_transpose(image)
    image = _normalise_mode(image)
    image = _to_srgb(image, icc_profile)
    image = _final_mode(image)
    image = _downscale(image, max_width)

    # A fresh image from raw pixels: nothing from ``info`` can leak into the save.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    buffer = io.BytesIO()
    clean.save(buffer, format="WEBP", quality=quality, method=6, exif=b"")
    output = buffer.getvalue()
    assert_clean_webp(output)
    return ProcessedImage(
        data=output,
        width=clean.width,
        height=clean.height,
        sha256=hashlib.sha256(output).hexdigest(),
    )


def _looks_like_heif(data: bytes) -> bool:
    """ISO-BMFF ``ftyp`` box naming a HEIF/HEIC brand (AVIF excluded)."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(data[0:4], "big")
    end = min(len(data), box_size if 16 <= box_size <= 4096 else 16)
    brands = {data[8:12]} | {data[i : i + 4] for i in range(16, end - 3, 4)}
    if brands & _AVIF_BRANDS:
        return False
    return bool(brands & _HEIF_BRANDS)


def _open(data: bytes, max_width: int) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data), formats=_DECODERS)
    except Image.DecompressionBombError:
        raise ImageRejected(_TOO_MANY_PIXELS) from None
    except Exception:  # untrusted input: any decoder failure is a rejection
        raise ImageRejected(_UNREADABLE) from None

    if image.format not in _FORMATS:
        raise ImageRejected(_UNREADABLE)
    width, height = image.size
    if width < 1 or height < 1:
        raise ImageRejected(_UNREADABLE)
    if width * height > MAX_PIXELS:
        raise ImageRejected(_TOO_MANY_PIXELS)
    try:
        frames = getattr(image, "n_frames", 1)
    except Exception:
        raise ImageRejected(_UNREADABLE) from None
    if image.format != "MPO" and frames > 1:
        raise ImageRejected(_ANIMATED)

    if image.format in ("JPEG", "MPO"):
        # Let libjpeg decode big photos at 1/2, 1/4 or 1/8 scale; both sides
        # stay >= max_width, so rotation by EXIF later cannot make it too small.
        image.draft(image.mode, (max_width, max_width))
    try:
        image.load()
    except Image.DecompressionBombError:
        raise ImageRejected(_TOO_MANY_PIXELS) from None
    except Exception:
        raise ImageRejected(_UNREADABLE) from None
    return image


def _exif_transpose(image: Image.Image) -> Image.Image:
    try:
        return ImageOps.exif_transpose(image)
    except Exception:  # broken EXIF: keep the stored orientation
        return image


def _normalise_mode(image: Image.Image) -> Image.Image:
    """Bring any decoded mode to RGB, RGBA, L or CMYK."""
    mode = image.mode
    if mode in ("RGB", "RGBA", "CMYK"):
        if mode == "RGB" and "transparency" in image.info:
            return image.convert("RGBA")
        return image
    if mode == "L":
        return image.convert("RGBA") if "transparency" in image.info else image
    if mode == "P":
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    if mode in ("PA", "LA", "La", "RGBa"):
        return image.convert("RGBA")
    if mode == "1":
        return image.convert("L")
    if mode.startswith("I;16") or mode == "I":
        # 16-bit grey: scale to 8 bits instead of clipping everything above 255.
        return image.convert("I").point(lambda value: value * (1 / 257)).convert("L")
    try:
        return image.convert("RGB")
    except Exception:
        raise ImageRejected(_UNREADABLE) from None


def _to_srgb(image: Image.Image, icc_profile: object) -> Image.Image:
    """Convert from an embedded ICC profile to sRGB; keep pixels as-is on failure."""
    if not icc_profile or not isinstance(icc_profile, bytes):
        return image
    alpha = image.getchannel("A") if image.mode == "RGBA" else None
    colour = image.convert("RGB") if alpha is not None else image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        converted = ImageCms.profileToProfile(colour, source, _SRGB, outputMode="RGB")
    except Exception:
        return image
    if converted is None:
        return image
    if alpha is not None:
        converted.putalpha(alpha)
    return converted


def _final_mode(image: Image.Image) -> Image.Image:
    """RGBA only when some pixel is actually transparent, else RGB."""
    if image.mode == "RGBA":
        if image.getchannel("A").getextrema() == (255, 255):
            return image.convert("RGB")
        return image
    if image.mode != "RGB":
        try:
            return image.convert("RGB")
        except Exception:
            raise ImageRejected(_UNREADABLE) from None
    return image


def _downscale(image: Image.Image, max_width: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, max_width / width, WEBP_MAX_DIMENSION / height)
    if scale >= 1.0:
        return image
    size = (
        max(1, min(max_width, round(width * scale))),
        max(1, min(WEBP_MAX_DIMENSION, round(height * scale))),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def assert_clean_webp(data: bytes) -> None:
    """Raise ValueError unless ``data`` is a still WebP with no metadata chunks.

    Allowed chunks: ``VP8 ``, ``VP8L``, ``VP8X``, ``ALPH``. A ``VP8X`` header
    must not announce ICC, EXIF, XMP or animation. Exactly one bitstream chunk.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a RIFF/WEBP file")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise ValueError("RIFF size does not match the data")

    offset = 12
    seen: list[bytes] = []
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("truncated chunk header")
        fourcc = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + size
        if payload_end > len(data):
            raise ValueError(f"truncated chunk {fourcc!r}")
        if fourcc not in _WEBP_ALLOWED_CHUNKS:
            raise ValueError(f"chunk {fourcc!r} is not allowed")
        if fourcc == b"VP8X":
            if seen or size < 10:
                raise ValueError("malformed VP8X chunk")
            flags = data[payload_start]
            if flags & (_VP8X_ICC | _VP8X_EXIF | _VP8X_XMP | _VP8X_ANIMATION):
                raise ValueError("VP8X announces metadata or animation")
        seen.append(fourcc)
        offset = payload_end + (size & 1)  # chunks are padded to even sizes

    bitstreams = [fourcc for fourcc in seen if fourcc in (b"VP8 ", b"VP8L")]
    if len(bitstreams) != 1:
        raise ValueError("expected exactly one image bitstream")
    if b"ALPH" in seen and b"VP8X" not in seen:
        raise ValueError("ALPH chunk without VP8X")


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def image_filename(img: ProcessedImage, date: str) -> str:
    """``YYYYMMDD-<first 10 hex of sha256>.webp`` for a ``YYYY-MM-DD`` date."""
    match = _DATE_RE.fullmatch(date)
    if not match or not re.fullmatch(r"[0-9a-f]{64}", img.sha256):
        raise ValueError("bad date or digest")
    return f"{match[1]}{match[2]}{match[3]}-{img.sha256[:10]}.webp"

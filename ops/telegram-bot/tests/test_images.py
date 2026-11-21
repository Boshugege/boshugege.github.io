import hashlib
import io
import random
import re
import struct
import unittest
import zlib

from PIL import Image, ImageCms, PngImagePlugin

from pnc_bot.images import (
    HEIC_MESSAGE,
    IMAGE_NAME_RE,
    MAX_INPUT_BYTES,
    ImageRejected,
    ProcessedImage,
    assert_clean_webp,
    image_filename,
    process_image,
)

# --------------------------------------------------------------------------
# fixtures built in memory

D50 = (0.9642, 1.0, 0.8249)
SRGB_PRIMARIES = ((0.4361, 0.2225, 0.0139), (0.3851, 0.7169, 0.0971), (0.1431, 0.0606, 0.7141))
P3_PRIMARIES = ((0.5151, 0.2412, -0.0011), (0.2919, 0.6922, 0.0419), (0.1571, 0.0666, 0.7841))
# Red and green primaries swapped: (255, 0, 0) in this space is sRGB green.
SWAPPED_PRIMARIES = (SRGB_PRIMARIES[1], SRGB_PRIMARIES[0], SRGB_PRIMARIES[2])

SECRETS = (b"SecretCam", b"Model-X9", b"SecretXMP", b"secret comment", b"SecretText", b"TEST-ICC")


def s15(value: float) -> bytes:
    return struct.pack(">i", round(value * 65536))


def make_rgb_icc(primaries, gamma: float = 2.2, description: str = "TEST-ICC display") -> bytes:
    """A minimal ICC v2 matrix/TRC RGB display profile."""
    def xyz(values):
        return b"XYZ " + b"\0" * 4 + b"".join(s15(v) for v in values)

    ascii_desc = description.encode("ascii") + b"\0"
    desc = (
        b"desc" + b"\0" * 4 + struct.pack(">I", len(ascii_desc)) + ascii_desc
        + struct.pack(">II", 0, 0) + struct.pack(">H", 0) + b"\0" + b"\0" * 67
    )
    curve = b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", round(gamma * 256)) + b"\0\0"
    tags = [
        (b"desc", desc),
        (b"cprt", b"text" + b"\0" * 4 + b"none\0"),
        (b"wtpt", xyz(D50)),
        (b"rXYZ", xyz(primaries[0])),
        (b"gXYZ", xyz(primaries[1])),
        (b"bXYZ", xyz(primaries[2])),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, blobs = b"", b""
    for signature, payload in tags:
        while len(payload) % 4:
            payload += b"\0"
        table += signature + struct.pack(">II", offset + len(blobs), len(payload))
        blobs += payload
    body = struct.pack(">I", len(tags)) + table + blobs
    size = 128 + len(body)
    header = (
        struct.pack(">I", size) + b"\0" * 4 + struct.pack(">I", 0x02100000) + b"mntr" + b"RGB " + b"XYZ "
        + struct.pack(">6H", 2026, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + b"\0" * 8
        + struct.pack(">I", 0) + b"".join(s15(v) for v in D50) + b"\0" * 4 + b"\0" * 16 + b"\0" * 28
    )
    assert len(header) == 128
    return header + body


P3_ICC = make_rgb_icc(P3_PRIMARIES, description="TEST-ICC Display P3")
SWAPPED_ICC = make_rgb_icc(SWAPPED_PRIMARIES, description="TEST-ICC swapped")

XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>SecretXMP'
    b"</dc:creator></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
)


def exif_bytes(orientation: int | None = None) -> bytes:
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    exif[0x010F] = "SecretCam"
    exif[0x0110] = "Model-X9"
    exif[0x8825] = {  # GPS IFD
        1: "N",
        2: (39.0, 54.0, 27.0),
        3: "E",
        4: (116.0, 23.0, 17.0),
    }
    return exif.tobytes()


def with_xmp_segment(jpeg: bytes) -> bytes:
    namespace = b"http://ns.adobe.com/xap/1.0/\0"
    segment = b"\xff\xe1" + struct.pack(">H", 2 + len(namespace) + len(XMP)) + namespace + XMP
    return jpeg[:2] + segment + jpeg[2:]


def two_tone(size=(200, 100), left=(220, 30, 30), right=(30, 30, 220), mode="RGB") -> Image.Image:
    image = Image.new(mode, size, right)
    image.paste(left, (0, 0, size[0] // 2, size[1]))
    return image


def encode(image: Image.Image, fmt: str, **params) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **params)
    return buffer.getvalue()


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_header_only(width: int, height: int) -> bytes:
    """A tiny PNG that claims huge dimensions (decompression bomb shape)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(b"\0" * 64))
        + png_chunk(b"IEND", b"")
    )


def webp_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    chunks, offset = [], 12
    while offset < len(data):
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        chunks.append((data[offset : offset + 4], data[offset + 8 : offset + 8 + size]))
        offset += 8 + size + (size & 1)
    return chunks


def build_webp(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"WEBP"
    for kind, payload in chunks:
        body += kind + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


class Helpers(unittest.TestCase):
    def assert_clean_output(self, result: ProcessedImage) -> Image.Image:
        assert_clean_webp(result.data)
        for secret in SECRETS:
            self.assertNotIn(secret, result.data)
        self.assertEqual(result.sha256, hashlib.sha256(result.data).hexdigest())
        image = decode(result.data)
        self.assertEqual(image.format, "WEBP")
        self.assertEqual((image.width, image.height), (result.width, result.height))
        for key in ("exif", "icc_profile", "xmp"):
            self.assertFalse(image.info.get(key), key)
        self.assertEqual(len(image.getexif()), 0)
        return image

    def assert_rejected(self, data: bytes, message: str | None = None) -> str:
        with self.assertRaises(ImageRejected) as caught:
            process_image(data)
        text = str(caught.exception)
        if message is not None:
            self.assertEqual(text, message)
        self.assertTrue(text)
        self.assertRegex(text, r"[一-鿿]")  # a Chinese sentence for the user
        return text


class FixtureSanity(unittest.TestCase):
    """The fixtures really carry the metadata the pipeline must remove."""

    def test_icc_profiles_are_accepted_by_littlecms(self):
        for icc in (P3_ICC, SWAPPED_ICC):
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            self.assertIn("TEST-ICC", ImageCms.getProfileDescription(profile))

    def test_jpeg_fixture_has_exif_gps_xmp_icc_comment(self):
        data = with_xmp_segment(encode(two_tone(), "JPEG", quality=95, exif=exif_bytes(6), icc_profile=P3_ICC, comment=b"secret comment"))
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        self.assertEqual(exif[0x0112], 6)
        self.assertEqual(exif.get_ifd(0x8825)[1], "N")
        self.assertEqual(image.info["icc_profile"], P3_ICC)
        self.assertIn(b"SecretXMP", image.info["xmp"] if isinstance(image.info["xmp"], bytes) else image.info["xmp"].encode())
        self.assertEqual(image.info["comment"], b"secret comment")


class ProcessJpeg(Helpers):
    def setUp(self):
        self.jpeg = with_xmp_segment(
            encode(two_tone(), "JPEG", quality=95, exif=exif_bytes(6), icc_profile=P3_ICC, comment=b"secret comment")
        )

    def test_metadata_is_gone(self):
        self.assert_clean_output(process_image(self.jpeg))

    def test_orientation_6_is_applied(self):
        result = process_image(self.jpeg)
        image = self.assert_clean_output(result).convert("RGB")
        self.assertEqual((result.width, result.height), (100, 200))  # rotated 90°
        top, bottom = image.getpixel((50, 50)), image.getpixel((50, 150))
        self.assertGreater(top[0], top[2] + 100, top)  # the stored left (red) half is now on top
        self.assertGreater(bottom[2], bottom[0] + 100, bottom)

    def test_display_p3_is_converted_to_srgb(self):
        flat = Image.new("RGB", (64, 64), (180, 120, 60))
        tagged = process_image(encode(flat, "JPEG", quality=100, icc_profile=P3_ICC))
        untagged = process_image(encode(flat, "JPEG", quality=100))
        a = decode(tagged.data).convert("RGB").getpixel((32, 32))
        b = decode(untagged.data).convert("RGB").getpixel((32, 32))
        self.assertGreaterEqual(max(abs(x - y) for x, y in zip(a, b)), 5, (a, b))
        self.assertGreater(a[0], b[0])  # P3 orange is more saturated than the same numbers in sRGB

    def test_swapped_primaries_profile_turns_red_into_green(self):
        data = encode(Image.new("RGB", (32, 32), (255, 0, 0)), "JPEG", quality=100, icc_profile=SWAPPED_ICC)
        pixel = decode(process_image(data).data).convert("RGB").getpixel((16, 16))
        self.assertGreater(pixel[1], 200, pixel)
        self.assertLess(pixel[0], 60, pixel)

    def test_broken_icc_profile_is_ignored(self):
        data = encode(Image.new("RGB", (32, 32), (10, 200, 30)), "JPEG", quality=100, icc_profile=b"not a profile" * 20)
        result = process_image(data)
        pixel = decode(result.data).convert("RGB").getpixel((16, 16))
        self.assertGreater(pixel[1], 150)
        self.assertNotIn(b"not a profile", result.data)

    def test_cmyk_jpeg(self):
        data = encode(Image.new("CMYK", (40, 40), (0, 255, 255, 0)), "JPEG", quality=95)
        image = self.assert_clean_output(process_image(data)).convert("RGB")
        r, g, b = image.getpixel((20, 20))
        self.assertGreater(r, 180)
        self.assertLess(g, 90)

    def test_mpo_uses_the_first_frame(self):
        first, second = Image.new("RGB", (60, 40), (250, 0, 0)), Image.new("RGB", (30, 20), (0, 0, 250))
        data = encode(first, "MPO", save_all=True, append_images=[second], exif=exif_bytes())
        self.assertEqual(Image.open(io.BytesIO(data)).format, "MPO")
        result = process_image(data)
        image = self.assert_clean_output(result).convert("RGB")
        self.assertEqual((result.width, result.height), (60, 40))
        self.assertGreater(image.getpixel((30, 20))[0], 200)

    def test_large_jpeg_is_downscaled_with_draft(self):
        # Both sides >= 2 x 1600, so libjpeg decodes at half size before LANCZOS.
        data = encode(two_tone((4000, 3400)), "JPEG", quality=80)
        result = process_image(data)
        self.assertEqual((result.width, result.height), (1600, 1360))
        image = decode(result.data).convert("RGB")
        self.assertGreater(image.getpixel((400, 680))[0], 180)
        self.assertGreater(image.getpixel((1200, 680))[2], 180)


class ProcessPng(Helpers):
    def test_png_with_text_exif_icc_and_alpha(self):
        image = Image.new("RGBA", (64, 32), (255, 0, 0, 255))
        image.paste((0, 0, 255, 0), (32, 0, 64, 32))  # right half fully transparent
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "SecretText")
        info.add_text("Author", "SecretText", zip=True)
        info.add_itxt("Description", "SecretText", lang="en", tkey="Description")
        data = encode(image, "PNG", pnginfo=info, exif=exif_bytes(), icc_profile=SWAPPED_ICC)
        source = Image.open(io.BytesIO(data))
        source.load()
        self.assertIn("Comment", source.info)
        self.assertTrue(source.getexif())
        self.assertIn(b"eXIf", data)

        result = process_image(data)
        out = self.assert_clean_output(result)
        self.assertEqual(out.mode, "RGBA")
        opaque, transparent = out.getpixel((8, 16)), out.getpixel((56, 16))
        self.assertGreater(opaque[3], 250)
        self.assertLess(transparent[3], 5)
        self.assertGreater(opaque[1], 200, opaque)  # converted through the swapped profile
        self.assertLess(opaque[0], 60, opaque)
        kinds = [kind for kind, _ in webp_chunks(result.data)]
        self.assertIn(b"ALPH", kinds)

    def test_fully_opaque_alpha_is_dropped(self):
        data = encode(Image.new("RGBA", (16, 16), (0, 128, 0, 255)), "PNG")
        result = process_image(data)
        self.assertEqual(self.assert_clean_output(result).mode, "RGB")
        self.assertNotIn(b"ALPH", result.data)

    def test_palette_with_transparency(self):
        image = Image.new("P", (16, 16), 0)
        image.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
        image.paste(1, (8, 0, 16, 16))
        data = encode(image, "PNG", transparency=1)
        out = self.assert_clean_output(process_image(data))
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((12, 8))[3], 0)

    def test_sixteen_bit_grey_is_scaled_not_clipped(self):
        data = encode(Image.new("I;16", (32, 32), 32768), "PNG")
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "I;16")
        value = self.assert_clean_output(process_image(data)).convert("L").getpixel((16, 16))
        self.assertTrue(110 <= value <= 145, value)

    def test_other_modes_and_formats(self):
        cases = {
            "grey jpeg": encode(Image.new("L", (20, 20), 100), "JPEG"),
            "grey+alpha png": encode(Image.new("LA", (20, 20), (100, 128)), "PNG"),
            "1-bit bmp": encode(Image.new("1", (20, 20), 1), "BMP"),
            "rgb bmp": encode(Image.new("RGB", (20, 20), (1, 2, 3)), "BMP"),
            "tiff": encode(Image.new("RGB", (20, 20), (1, 2, 3)), "TIFF"),
            "single-frame gif": encode(Image.new("P", (20, 20), 3), "GIF"),
            "webp with exif and icc": encode(Image.new("RGB", (20, 20), (9, 9, 9)), "WEBP", exif=exif_bytes(), icc_profile=P3_ICC),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                result = self.assert_clean_output(process_image(data))
                self.assertEqual((result.width, result.height), (20, 20))


class ProcessSizes(Helpers):
    def test_oversize_width_is_scaled_down(self):
        data = encode(two_tone((4000, 1000)), "PNG")
        result = process_image(data)
        self.assertEqual((result.width, result.height), (1600, 400))
        self.assert_clean_output(result)

    def test_custom_max_width(self):
        result = process_image(encode(two_tone((1000, 500)), "PNG"), max_width=800)
        self.assertEqual((result.width, result.height), (800, 400))

    def test_small_images_are_not_upscaled(self):
        result = process_image(encode(two_tone((300, 50)), "PNG"))
        self.assertEqual((result.width, result.height), (300, 50))

    def test_very_tall_image_fits_webp_limit(self):
        result = process_image(encode(Image.new("L", (100, 20000), 50), "PNG"))
        self.assertLessEqual(result.height, 16383)
        self.assertEqual(result.height, 16383)
        self.assert_clean_output(result)

    def test_output_is_deterministic(self):
        data = encode(two_tone(), "PNG")
        self.assertEqual(process_image(data), process_image(data))

    def test_quality_and_width_are_validated(self):
        data = encode(two_tone(), "PNG")
        with self.assertRaises(ValueError):
            process_image(data, quality=0)
        with self.assertRaises(ValueError):
            process_image(data, max_width=0)


class ProcessRejections(Helpers):
    def test_input_too_large(self):
        text = self.assert_rejected(b"\xff\xd8" + b"\0" * MAX_INPUT_BYTES)
        self.assertIn("20 MB", text)

    def test_animated_gif(self):
        frames = [Image.new("RGB", (20, 20), (255, 0, 0)), Image.new("RGB", (20, 20), (0, 0, 255))]
        data = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
        self.assertGreater(Image.open(io.BytesIO(data)).n_frames, 1)
        self.assertIn("动图", self.assert_rejected(data))

    def test_animated_webp_and_apng_and_multipage_tiff(self):
        frames = [Image.new("RGB", (20, 20), (255, 0, 0)), Image.new("RGB", (20, 20), (0, 0, 255))]
        for fmt in ("WEBP", "PNG", "TIFF"):
            with self.subTest(fmt=fmt):
                data = encode(frames[0], fmt, save_all=True, append_images=frames[1:])
                self.assertIn("动图", self.assert_rejected(data))

    def test_garbage_bytes(self):
        rng = random.Random(1)
        for data in (
            b"",
            b"hello, this is not an image" * 50,
            bytes(rng.getrandbits(8) for _ in range(5000)),
            b"\xff\xd8\xff\xe0" + bytes(rng.getrandbits(8) for _ in range(500)),
            b"\x89PNG\r\n\x1a\n" + bytes(500),
            b"RIFF\x10\x00\x00\x00WEBPVP8 " + bytes(100),
        ):
            with self.subTest(prefix=data[:12]):
                self.assert_rejected(data)

    def test_truncated_jpeg(self):
        data = encode(two_tone((400, 400)), "JPEG", quality=95)
        self.assert_rejected(data[: len(data) // 2])

    def test_decompression_bomb_guard(self):
        for width, height in ((8000, 8000), (20000, 20000), (60000, 60000)):
            with self.subTest(size=(width, height)):
                text = self.assert_rejected(png_header_only(width, height))
                self.assertIn("像素", text)

    def test_heic_like_input(self):
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + bytes(200)
        self.assert_rejected(heic, HEIC_MESSAGE)
        mif1 = b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1heixmiaf" + bytes(200)
        self.assert_rejected(mif1, HEIC_MESSAGE)
        self.assertEqual(HEIC_MESSAGE, "服务器不支持 HEIC，请用“照片”方式发送（会自动转成 JPEG）")

    def test_avif_is_not_called_heic(self):
        avif = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + bytes(200)
        self.assertNotEqual(self.assert_rejected(avif), HEIC_MESSAGE)

    def test_formats_outside_the_allow_list(self):
        image = Image.new("RGB", (16, 16), (1, 2, 3))
        for fmt in ("ICO", "PPM", "PCX", "TGA"):
            with self.subTest(fmt=fmt):
                self.assert_rejected(encode(image, fmt))


class AssertCleanWebp(unittest.TestCase):
    def setUp(self):
        self.rgb = process_image(encode(Image.new("RGB", (32, 32), (0, 99, 0)), "PNG")).data
        rgba = Image.new("RGBA", (32, 32), (0, 99, 0, 255))
        rgba.paste((0, 0, 0, 0), (0, 0, 16, 16))
        self.rgba = process_image(encode(rgba, "PNG")).data

    def test_clean_outputs_pass(self):
        assert_clean_webp(self.rgb)
        assert_clean_webp(self.rgba)
        assert_clean_webp(encode(Image.new("RGBA", (8, 8), (1, 2, 3, 4)), "WEBP", lossless=True))

    def test_webp_with_metadata_is_rejected(self):
        image = Image.new("RGB", (16, 16), (5, 5, 5))
        for params in ({"exif": exif_bytes()}, {"icc_profile": P3_ICC}, {"xmp": XMP}):
            with self.subTest(params=list(params)):
                with self.assertRaises(ValueError):
                    assert_clean_webp(encode(image, "WEBP", **params))

    def test_animated_webp_is_rejected(self):
        frames = [Image.new("RGB", (8, 8), (255, 0, 0)), Image.new("RGB", (8, 8), (0, 0, 255))]
        with self.assertRaises(ValueError):
            assert_clean_webp(encode(frames[0], "WEBP", save_all=True, append_images=frames[1:]))

    def test_unknown_or_metadata_chunk_appended(self):
        for kind in (b"EXIF", b"XMP ", b"ICCP", b"JUNK"):
            with self.subTest(kind=kind):
                data = build_webp(webp_chunks(self.rgb) + [(kind, b"SecretCam!")])
                with self.assertRaises(ValueError):
                    assert_clean_webp(data)

    def test_vp8x_flags_are_checked(self):
        chunks = webp_chunks(self.rgba)
        self.assertEqual(chunks[0][0], b"VP8X")
        assert_clean_webp(build_webp(chunks))  # rebuilt unchanged: still fine
        for flag in (0x20, 0x08, 0x04, 0x02):
            with self.subTest(flag=hex(flag)):
                payload = bytes([chunks[0][1][0] | flag]) + chunks[0][1][1:]
                with self.assertRaises(ValueError):
                    assert_clean_webp(build_webp([(b"VP8X", payload)] + chunks[1:]))

    def test_structural_errors(self):
        chunks = webp_chunks(self.rgb)
        bitstream = [chunk for chunk in chunks if chunk[0] in (b"VP8 ", b"VP8L")]
        alph = [chunk for chunk in webp_chunks(self.rgba) if chunk[0] == b"ALPH"]
        broken = {
            "empty": b"",
            "not riff": b"RIFX" + self.rgb[4:],
            "not webp": self.rgb[:8] + b"WAVE" + self.rgb[12:],
            "size too big": self.rgb[:4] + struct.pack("<I", len(self.rgb)) + self.rgb[8:],
            "trailing junk": self.rgb + b"\0\0",
            "truncated": self.rgb[:4] + struct.pack("<I", len(self.rgb) - 18) + self.rgb[8:-10],
            "two bitstreams": build_webp(bitstream + bitstream),
            "no bitstream": build_webp([]),
            "alph without vp8x": build_webp(alph + bitstream),
            "vp8x not first": build_webp(bitstream + [(b"VP8X", bytes(10))]),
        }
        for name, data in broken.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    assert_clean_webp(data)


class Filenames(unittest.TestCase):
    def test_image_filename(self):
        result = process_image(encode(two_tone(), "PNG"))
        name = image_filename(result, "2026-11-21")
        self.assertEqual(name, f"20261121-{result.sha256[:10]}.webp")
        self.assertRegex(name, IMAGE_NAME_RE)
        self.assertTrue(re.match(IMAGE_NAME_RE, name))

    def test_bad_inputs(self):
        result = ProcessedImage(b"x", 1, 1, "a" * 64)
        for date in ("2026-1-1", "20261121", "2026-11-21x", ""):
            with self.subTest(date=date), self.assertRaises(ValueError):
                image_filename(result, date)
        with self.assertRaises(ValueError):
            image_filename(ProcessedImage(b"x", 1, 1, "../" + "a" * 61), "2026-11-21")

    def test_name_regex(self):
        for name in ("20261121-0123456789.webp",):
            self.assertRegex(name, IMAGE_NAME_RE)
        for name in ("20261121-0123456789.png", "2026-1121-0123456789.webp", "20261121-ABCDEF0123.webp", "../x.webp"):
            self.assertNotRegex(name, IMAGE_NAME_RE)


if __name__ == "__main__":
    unittest.main()

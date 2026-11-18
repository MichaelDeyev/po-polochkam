"""Открытие снимков, отпечатки и миниатюры. Без scipy: это заметно уменьшает программу."""
import io
import os
import subprocess
import zlib

import numpy as np
from PIL import Image, ImageOps

from .paths import exiftool_path, long_path

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

Image.MAX_IMAGE_PIXELS = 500_000_000
RAW_EXT = {".dng", ".cr2", ".nef", ".arw"}
NO_WINDOW = 0x08000000
_N = 32
_COS = np.cos(np.pi * np.outer(np.arange(_N), 2 * np.arange(_N) + 1) / (2 * _N))


def _raw_preview(path):
    for name in ("-JpgFromRaw", "-PreviewImage"):
        out = subprocess.run([exiftool_path(), "-charset", "filename=utf8", "-b", name, path], capture_output=True,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=120).stdout
        if out:
            return Image.open(io.BytesIO(out))
    raise OSError("в файле нет встроенного снимка")


def open_image(root, rel):
    path = long_path(root, rel)
    if os.path.splitext(rel)[1].lower() in RAW_EXT:
        return _raw_preview(os.path.join(os.path.abspath(root), rel))
    return Image.open(path)


def _hex(bits):
    return f"{int(''.join('1' if b else '0' for b in bits.ravel()), 2):016x}"


def phash(im):
    a = np.asarray(im.convert("L").resize((_N, _N), Image.LANCZOS), dtype=np.float64)
    low = (_COS @ a @ _COS.T)[:8, :8]
    return _hex(low > np.median(low))


def dhash(im):
    a = np.asarray(im.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    return _hex(a[:, 1:] > a[:, :-1])


def scene(im):
    """Цвета кадра и доля белого: у одной сцены цвета совпадают, даже когда поза другая."""
    small = im.copy()
    small.thumbnail((128, 128))
    a = np.asarray(small, dtype=np.uint8)
    q = (a // 32).astype(np.int32)
    h = np.bincount((q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]).ravel(), minlength=512).astype(np.uint16)
    return zlib.compress(h.tobytes()), float((a.min(axis=2) > 225).mean())


def hist_array(blob):
    h = np.frombuffer(zlib.decompress(blob), dtype=np.uint16).astype(np.float32)
    return h / max(h.sum(), 1)


def fingerprint(job):
    """Выполняется в отдельном процессе: (id, phash, dhash, w, h, цвета, доля белого) или None при сбое."""
    file_id, root, rel = job
    try:
        with open_image(root, rel) as im:
            w, h = im.size
            im.draft("RGB", (256, 256))     # JPEG декодируется сразу уменьшенным — в разы быстрее
            im = ImageOps.exif_transpose(im).convert("RGB")
            if im.width > im.height:        # ориентация не важна для сравнения размеров
                w, h = max(w, h), min(w, h)
            else:
                w, h = min(w, h), max(w, h)
            blob, white = scene(im)
            return file_id, phash(im), dhash(im), w, h, blob, white
    except Exception:       # битый файл, неизвестный формат
        return file_id, None, None, 0, 0, None, 1.0


def make_thumb(root, rel, out, px=720):
    with open_image(root, rel) as im:
        im.draft("RGB", (px * 2, px * 2))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((px, px))
        tmp = out + ".tmp"
        im.save(tmp, "JPEG", quality=80)
    os.replace(tmp, out)

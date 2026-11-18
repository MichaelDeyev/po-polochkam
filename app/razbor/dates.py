"""Дата съёмки из метаданных и имени файла."""
import datetime as dt
import re

MIN_DATE = dt.datetime(1990, 1, 1)
TAGS = ("SubSecDateTimeOriginal", "DateTimeOriginal", "CreationDate", "DateCreated", "CreateDate", "GPSDateTime",
        "ModifyDate", "Make", "Model", "UserComment", "ContentIdentifier")
STRONG = ("EXIF", "видео")
WEAK = "слабый"


def max_date():
    return dt.datetime.now() + dt.timedelta(days=1)


def tag(m, *names):
    """Первое непустое значение тега; имя — 'Группа:Тег' или просто 'Тег' (любая группа)."""
    for name in names:
        if ":" in name:
            v = m.get(name)
        else:
            v = next((m[k] for k in m if k.split(":", 1)[-1] == name), None)
        if v not in (None, "", "-"):
            return v
    return None


def parse_dt(v):
    if not isinstance(v, str):
        return None
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})(?: (\d{2}):(\d{2})(?::(\d{2}))?)?", v.strip())
    if not m:
        return None
    try:
        d = dt.datetime(*[int(x) if x else 0 for x in m.groups()])
    except ValueError:
        return None
    # 01.01.2000/2001 00:00 — сброшенные часы камеры или пустая GPS-дата, а не съёмка.
    if d.year <= 2002 and (d.month, d.day) == (1, 1):
        return None
    return d if MIN_DATE <= d <= max_date() else None


FILENAME_PATTERNS = [
    # IMG_20180512_143307, VID_20180512_143307, 20150320_160505, PXL_20210101_101010123
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})"),
    # Screenshot_2018-05-12-14-33-07, photo_2018-05-12_14-33-07, Снимок экрана 2018-05-12 в 14.33.07
    re.compile(r"((?:19|20)\d{2})-(\d{2})-(\d{2})[ _\-T]?(?:в |at )?(\d{2})[.\-:](\d{2})[.\-:](\d{2})"),
    # IMG-20180512-WA0001 (только дата)
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})-WA"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)"),
]


def date_from_name(name):
    for p in FILENAME_PATTERNS:
        m = p.search(name)
        if m:
            try:
                d = dt.datetime(*[int(x) for x in m.groups()])
            except ValueError:
                continue
            if MIN_DATE <= d <= max_date():
                return d
    return None


def pick_date(m, name, transfer_days):
    """(дата, источник, конфликт). Источник 'слабый' — дата годится только для _Проверить.

    transfer_days — дни массового переноса: в такие дни у тысяч файлов разом
    переписалась дата, и дата контейнера видео, попавшая на такой день, ничего не значит.
    """
    cands = []
    for src, val in (
        ("EXIF дата съёмки", tag(m, "Composite:SubSecDateTimeOriginal", "ExifIFD:DateTimeOriginal", "DateTimeOriginal")),
        ("видео: дата съёмки iPhone", tag(m, "Keys:CreationDate", "UserData:CreationDate", "CreationDate")),
        ("XMP/IPTC", tag(m, "XMP-photoshop:DateCreated", "XMP-exif:DateTimeOriginal", "IPTC:DateCreated", "XMP-xmp:CreateDate")),
        ("GPS", tag(m, "Composite:GPSDateTime", "GPSDateTime")),
    ):
        d = parse_dt(val)
        if d:
            cands.append((src, d))
    fn = date_from_name(name)
    if fn:
        cands.append(("имя файла", fn))
    qt = parse_dt(tag(m, "QuickTime:CreateDate"))
    if qt and qt.date() not in transfer_days:
        cands.append(("видео: дата контейнера", qt))

    if not cands:
        if qt:
            return qt, f"{WEAK}: дата видео = день переноса", None
        weak = parse_dt(tag(m, "ExifIFD:CreateDate", "IFD0:ModifyDate"))
        return (weak, f"{WEAK}: дата изменения в EXIF", None) if weak else (None, None, None)

    best_src, best = cands[0]
    conflict = None
    # Источники расходятся больше чем на сутки — берём более раннюю дату: снять раньше,
    # чем файл появился, нельзя, а перезапись при переносе даёт более позднюю.
    earliest = min(cands, key=lambda c: c[1])
    if abs((earliest[1] - best).total_seconds()) > 86400:
        conflict = f"{best_src} {best:%Y-%m-%d} ↔ {earliest[0]} {earliest[1]:%Y-%m-%d}"
        best_src, best = earliest
    return best, best_src, conflict

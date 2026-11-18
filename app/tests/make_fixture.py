"""Тестовая папка с искусственными снимками: случаи, на которых программа уже ошибалась бы."""
import os
import shutil
import sys

import numpy as np
from PIL import Image

import pillow_heif

pillow_heif.register_heif_opener()


def noise_photo(seed, size=(640, 480), tint=(0, 0, 0)):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 256, (6, 8, 3), dtype=np.uint8)
    a = np.clip(a.astype(np.int16) + np.array(tint, dtype=np.int16), 0, 255).astype(np.uint8)
    return Image.fromarray(a).resize(size, Image.BICUBIC)


def save(root, rel, im, date=None, make=None, **kw):
    path = "\\\\?\\" + os.path.join(os.path.abspath(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if date:
        exif.get_ifd(0x8769)[0x9003] = date
    im.save(path, exif=exif, quality=90, **kw)
    return path


def build(root):
    os.makedirs(root, exist_ok=True)
    # Разложить по датам
    a = save(root, "куча\\IMG_0001.jpg", noise_photo(1), "2019:05:06 10:11:12")
    save(root, "куча\\Дача 2020\\IMG_0002.jpg", noise_photo(2), "2020:07:01 12:00:00")
    save(root, "куча\\IMG_20180512_143307.jpg", noise_photo(3))     # дата только в имени
    save(root, "телефон\\фото с длинным именем " + "ю" * 120 + ".jpg", noise_photo(4), "2021:01:02 03:04:05")
    # Точная копия
    os.makedirs(os.path.join(root, "копии"), exist_ok=True)
    shutil.copy2(a, os.path.join(root, "копии", "IMG_0001 (1).jpg"))
    # Та же картинка, уменьшенная: должна уйти в похожие дубли
    save(root, "куча\\small_copy.jpg", noise_photo(1).resize((320, 240)), "2019:05:06 10:11:12")
    # Серия: соседние кадры одной съёмки с разной позой — не дубли, но группа
    base = noise_photo(10, tint=(40, 0, 0))
    for i in range(4):
        im = base.copy()
        im.paste(noise_photo(100 + i, (200, 200)), (40 + i * 90, 120))
        save(root, f"серия\\DSC{1000 + i}.jpg", im, f"2022:03:04 15:00:{10 + i:02d}")
    # Снимок с айфона
    save(root, "телефон\\IMG_5000.heic", noise_photo(8), "2024:08:09 18:30:00")
    # Скриншот, скан, снимок без даты
    save(root, "телефон\\Screenshot_2021-02-03-10-20-30.png", noise_photo(5, (360, 640)))
    save(root, "скан\\scan01.jpg", noise_photo(6), "2023:01:01 09:00:00", make="NORITSU KOKI")
    save(root, "куча\\без даты.jpg", noise_photo(7))
    # Правка айфона рядом с оригиналом: та же картинка, файл с буквой E
    save(root, "телефон\\IMG_0100.jpg", noise_photo(40), "2023:03:03 12:00:00")
    save(root, "телефон\\IMG_E0100.jpg", noise_photo(40, tint=(12, 12, 12)), "2023:03:03 12:00:00")
    # Без даты, но с подсказками: соседний номер кадра и год в названии папки
    save(root, "куча\\IMG_0003.jpg", noise_photo(30))
    save(root, "Поездка 2016\\море.jpg", noise_photo(31))
    # Папка «не трогать»
    save(root, "Свадьба\\кадр1.jpg", noise_photo(20), "2017:06:10 16:00:00")
    save(root, "Свадьба\\кадр2.jpg", noise_photo(21), "2017:06:10 16:00:05")
    # Видео, не медиа, мусор, битый файл
    with open(os.path.join(root, "телефон", "VID_20200101_101010.mp4"), "wb") as f:
        f.write(b"\0" * 2048)
    with open(os.path.join(root, "куча", "заметка.txt"), "w", encoding="utf-8") as f:
        f.write("не фото")
    with open(os.path.join(root, "куча", "._IMG_0001.jpg"), "wb") as f:
        f.write(b"mac")
    with open(os.path.join(root, "куча", "битый.jpg"), "wb") as f:
        f.write(b"not a jpeg")


if __name__ == "__main__":
    build(sys.argv[1])

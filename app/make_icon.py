"""Иконка программы «По полочкам»: python app\\make_icon.py
Рисует две фотокарточки на полке на оранжевой плитке (вариант, выбранный 07.11.2026) и сохраняет
razbor\\ui\\icon.ico (все размеры для Windows), razbor\\ui\\icon.png (значок страницы и окна)
и картинки установщика installer\\wizard-*.bmp (боковая — на первой и последней странице, маленькая — в шапке).
Готовые файлы лежат в git, запускать нужно только при изменении рисунка. Нужен Pillow.
"""
import os

from PIL import Image, ImageDraw, ImageFilter

S = 1024
HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "razbor", "ui")
INSTALLER = os.path.join(HERE, "installer")
TOP_COLOR, BOTTOM_COLOR = (255, 190, 80), (240, 128, 40)
# Inno Setup сам выбирает картинку по масштабу экрана: 100 %, 150 %, 200 %
WIZARD_SIZES = [(164, 314), (246, 471), (328, 628)]
SMALL_SIZES = [55, 83, 110]
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]

SCENES = {
    "day": {"sky": ((110, 193, 240), (176, 225, 250)), "sun": (255, 201, 60), "hill": [(91, 181, 90), (60, 140, 70)]},
    "sea": {"sky": ((120, 200, 230), (200, 236, 245)), "sun": (255, 214, 90), "hill": [(40, 150, 190), (25, 110, 160)]},
}


def vgrad(w, h, top, bottom):
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return img


def picture(w, h, scene):
    """Снимок внутри карточки: небо, солнце, холмы."""
    s = SCENES[scene]
    img = vgrad(w, h, *s["sky"])
    d = ImageDraw.Draw(img)
    r = w * 0.13
    cx, cy = w * 0.7, h * 0.3
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=s["sun"])
    d.polygon([(0, h * 0.75), (w * 0.35, h * 0.42), (w * 0.62, h * 0.7), (w, h * 0.55), (w, h), (0, h)], fill=s["hill"][0])
    d.polygon([(0, h), (0, h * 0.85), (w * 0.5, h * 0.7), (w, h * 0.9), (w, h)], fill=s["hill"][1])
    return img


def card(w, h, scene):
    """Фотокарточка в белой рамке, снизу рамка шире."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(w * 0.05), fill=255)
    base = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    side = int(w * 0.08)
    base.paste(picture(w - 2 * side, int(h - side - h * 0.2), scene), (side, side))
    ImageDraw.Draw(base).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(w * 0.05), outline=(200, 205, 212, 255),
                                           width=max(2, w // 60))
    img.paste(base, (0, 0), mask)
    return img


def with_shadow(layer, blur=18, alpha=90, off=(10, 14)):
    a = layer.split()[3].point(lambda v: v * alpha // 255)
    sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sh.putalpha(a)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.paste(sh, off, sh)
    return Image.alpha_composite(out, layer)


def stand(canvas, img, angle, cx, floor):
    """Ставит карточку на полку: нижняя точка касается floor, середина — на cx."""
    r = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    box = r.split()[3].getbbox()
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(r, (int(cx - (box[0] + box[2]) / 2), int(floor - box[3])), r)
    return Image.alpha_composite(canvas, with_shadow(layer))


def art():
    """Две карточки на полке на прозрачном фоне."""
    c = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    floor = 800
    c = stand(c, card(400, 480, "sea"), 10, 640, floor)
    c = stand(c, card(450, 540, "day"), -7, 430, floor)
    d = ImageDraw.Draw(c)
    d.rounded_rectangle([120, floor - 6, S - 120, floor + 94], radius=33, fill=(150, 90, 45, 255))
    d.rounded_rectangle([120, floor + 56, S - 120, floor + 94], radius=25, fill=(105, 60, 30, 255))
    return c


def draw():
    """Иконка: оранжевая плитка со скруглёнными углами и рисунок."""
    g = vgrad(S, S, TOP_COLOR, BOTTOM_COLOR)
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([40, 40, S - 41, S - 41], radius=210, fill=255)
    c = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c.paste(g, (0, 0), m)
    return Image.alpha_composite(c, art())


def wizard_image(w, h):
    """Боковая картинка установщика: тот же оранжевый фон во всю высоту, рисунок чуть ниже середины."""
    out = vgrad(w, h, TOP_COLOR, BOTTOM_COLOR)
    a = art().crop((60, 150, S - 60, 910))     # без пустых полей вокруг рисунка
    side = int(w * 0.92)
    a = a.resize((side, int(side * a.height / a.width)), Image.LANCZOS)
    out.alpha_composite(a, ((w - a.width) // 2, int(h * 0.55 - a.height / 2)))
    return out.convert("RGB")


def small_image(s, big):
    """Значок в шапке установщика: иконка на белом фоне (картинки установщика без прозрачности)."""
    out = Image.new("RGBA", (s, s), (255, 255, 255, 255))
    out.alpha_composite(big.resize((s, s), Image.LANCZOS))
    return out.convert("RGB")


def main():
    big = draw()
    # каждый размер уменьшаем из большого рисунка: так мелкие значки чётче
    small = [big.resize((s, s), Image.LANCZOS) for s in ICO_SIZES]
    small[-1].save(os.path.join(UI, "icon.ico"), sizes=[(s, s) for s in ICO_SIZES], append_images=small[:-1])
    big.resize((192, 192), Image.LANCZOS).save(os.path.join(UI, "icon.png"), optimize=True)
    for w, h in WIZARD_SIZES:
        wizard_image(w, h).save(os.path.join(INSTALLER, f"wizard-{w}.bmp"))
    for s in SMALL_SIZES:
        small_image(s, big).save(os.path.join(INSTALLER, f"wizard-small-{s}.bmp"))
    print("готово:", os.path.join(UI, "icon.ico"), "и картинки в", INSTALLER)


if __name__ == "__main__":
    main()

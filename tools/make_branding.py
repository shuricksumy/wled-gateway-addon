#!/usr/bin/env python3
"""Generate the add-on's icon.png and logo.png.

Home Assistant picks these up by filename from the add-on folder: icon.png
(square) for the Supervisor panel, logo.png (~2.5:1) for the add-on store.
Keeping them generated rather than hand-drawn means the artwork is editable —
change a constant here and re-run instead of opening an image editor:

    python3 tools/make_branding.py

Needs Pillow (`pip install pillow`); nothing at runtime depends on this.
"""

import colorsys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "wled_gateway"

SS = 4  # supersampling factor — draw big, downsample for antialiased edges

BG_TOP = (24, 27, 36)
BG_BOTTOM = (10, 11, 16)
LED_COUNT = 6
HUE_SPAN = 0.82  # red → violet, stopping short of wrapping back to red

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def vertical_gradient(size, top, bottom):
    w, h = size
    column = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        column.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return column.resize((w, h), Image.BILINEAR)


def rounded_panel(size, radius):
    """A dark rounded tile with a faint top highlight, as the artwork's base."""
    panel = vertical_gradient(size, BG_TOP, BG_BOTTOM).convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    panel.putalpha(mask)

    edge = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, outline=(255, 255, 255, 26), width=max(1, size[0] // 200)
    )
    return Image.alpha_composite(panel, edge)


def led_colors(n):
    return [
        tuple(round(c * 255) for c in colorsys.hsv_to_rgb(i / max(1, n - 1) * HUE_SPAN, 0.88, 1.0))
        for i in range(n)
    ]


def draw_backing(canvas, box):
    """The strip's PCB under the LEDs — without it the chips read as loose
    floating pills instead of one lit strip."""
    x0, y0, x1, y1 = box
    height = y1 - y0
    pad_x = height * 0.42
    pad_y = height * 0.30
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y],
        radius=(height + pad_y * 2) * 0.30,
        fill=(38, 42, 54, 255),
        outline=(70, 77, 95, 255),
        width=max(1, int(height * 0.035)),
    )
    return Image.alpha_composite(canvas, layer)


def draw_strip(size, box, count=LED_COUNT):
    """The product itself: a lit LED strip, drawn as discrete chips so it reads
    as LEDs rather than as a plain gradient bar. Chips are kept wider than they
    are tall — taller ones look like capsules, not emitters."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    height = y1 - y0
    gap = (x1 - x0) * 0.030
    seg = ((x1 - x0) - gap * (count - 1)) / count
    for i, color in enumerate(led_colors(count)):
        sx = x0 + i * (seg + gap)
        draw.rounded_rectangle([sx, y0, sx + seg, y1], radius=height * 0.28, fill=color + (255,))
    return layer


def with_glow(base, layer, blur, passes=2):
    """Bloom: blurred copies of the lit strip composited under it, so the LEDs
    look like they're emitting light onto the dark tile."""
    out = base
    for _ in range(passes):
        glow = layer.filter(ImageFilter.GaussianBlur(blur))
        out = Image.alpha_composite(out, glow)
    return Image.alpha_composite(out, layer)


def make_icon(px=512):
    size = (px * SS, px * SS)
    canvas = rounded_panel(size, radius=int(px * SS * 0.22))

    w, h = size
    # Sized generously: the icon is displayed as small as 32px in places, where
    # a slimmer strip thins out into an unreadable coloured dash.
    strip_w = w * 0.78
    strip_h = h * 0.165
    box = ((w - strip_w) / 2, (h - strip_h) / 2, (w + strip_w) / 2, (h + strip_h) / 2)
    canvas = draw_backing(canvas, box)
    strip = draw_strip(size, box, count=5)

    canvas = with_glow(canvas, strip, blur=h * 0.07, passes=4)
    return canvas.resize((px, px), Image.LANCZOS)


def make_logo(width=500, height=200):
    size = (width * SS, height * SS)
    canvas = rounded_panel(size, radius=int(height * SS * 0.16))

    w, h = size
    strip_w = w * 0.80
    strip_h = h * 0.13
    top = h * 0.20
    box = ((w - strip_w) / 2, top, (w + strip_w) / 2, top + strip_h)
    canvas = draw_backing(canvas, box)
    strip = draw_strip(size, box, count=8)
    canvas = with_glow(canvas, strip, blur=h * 0.06, passes=3)

    draw = ImageDraw.Draw(canvas)
    font = load_font(int(h * 0.20))
    text = "WLED Gateway"
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((w - (bbox[2] - bbox[0])) / 2 - bbox[0], h * 0.52),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )

    sub_font = load_font(int(h * 0.095))
    sub = "LIVE PREVIEW FAN-OUT"
    sbox = draw.textbbox((0, 0), sub, font=sub_font)
    draw.text(
        ((w - (sbox[2] - sbox[0])) / 2 - sbox[0], h * 0.79),
        sub,
        font=sub_font,
        fill=(150, 160, 178, 255),
    )

    return canvas.resize((width, height), Image.LANCZOS)


def main():
    icon_path = OUT_DIR / "icon.png"
    logo_path = OUT_DIR / "logo.png"
    make_icon().save(icon_path)
    make_logo().save(logo_path)
    for p in (icon_path, logo_path):
        with Image.open(p) as im:
            print(f"wrote {p.relative_to(OUT_DIR.parent)} — {im.size[0]}x{im.size[1]} {im.mode}")


if __name__ == "__main__":
    main()

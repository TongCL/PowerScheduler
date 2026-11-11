# -*- coding: utf-8 -*-
"""生成应用图标 app.ico：蓝紫渐变圆角方块 + 白色「电源·时钟」一体符号。

设计语言：电源环同时是表盘——竖线（穿过顶部缺口）即分针指向 12 点，
另有时针指向 4 点半方向，环内 3/6/9 点位置有刻度点，寓意「定时开关机」。
运行：python make_icon.py  （需要 Pillow）
"""
import io
import math
import os
import struct

from PIL import Image, ImageDraw, ImageFilter

SS = 4          # 超采样倍率（画布 = 256 * SS）
S = 256 * SS
TOP = (78, 154, 255)    # 渐变起点 #4E9AFF
BOT = (110, 82, 255)    # 渐变终点 #6E52FF
WHITE = (255, 255, 255, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_gradient(radius):
    """对角蓝紫渐变 + 圆角蒙版（返回 RGBA，圆角外透明）。"""
    # 先在小画布上逐像素生成对角渐变，再放大（平滑且快）
    n = 257
    mini = Image.new("RGB", (n, n))
    px = mini.load()
    for y in range(n):
        for x in range(n):
            px[x, y] = lerp(TOP, BOT, (x + y) / (2 * (n - 1)))
    grad = mini.resize((S, S), Image.BILINEAR).convert("RGBA")

    # 左上柔光（先叠加，再上蒙版，避免溢出圆角）
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse([S * 0.06, -S * 0.18, S * 0.62, S * 0.34],
                               fill=(255, 255, 255, 88))
    hl = hl.filter(ImageFilter.GaussianBlur(S * 0.09))
    grad.alpha_composite(hl)

    mask = Image.new("L", (S, S), 0)
    m = 10 * SS
    ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=radius, fill=255)
    grad.putalpha(mask)
    return grad


def draw_glyph(img, cx, cy, r, stroke, with_ticks=True):
    """电源环（顶部缺口）+ 分针竖线 + 时针 + 刻度点，全部圆头。"""
    d = ImageDraw.Draw(img)

    def cap(x, y, w, color=WHITE):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)

    def dot(x, y, rad, color):
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=color)

    # 电源环：-55° → 235°，顶部留 70° 缺口
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-55, end=235,
          fill=WHITE, width=stroke)
    a1 = math.radians(-55)
    a2 = math.radians(235)
    cap(cx + r * math.cos(a1), cy + r * math.sin(a1), stroke)
    cap(cx + r * math.cos(a2), cy + r * math.sin(a2), stroke)

    # 竖线：从圆心向上穿过缺口（电源柄 = 分针指 12 点）
    top_y = cy - r - int(r * 0.18)
    d.line([(cx, cy), (cx, top_y)], fill=WHITE, width=stroke)
    cap(cx, top_y, stroke)

    # 时针：指向 4 点半方向（右下 45°）
    hl = int(r * 0.55)
    hx, hy = cx + int(hl * 0.7071), cy + int(hl * 0.7071)
    hw = stroke - 3 * SS
    d.line([(cx, cy), (hx, hy)], fill=WHITE, width=hw)
    cap(hx, hy, hw)

    # 轴心
    dot(cx, cy, int(stroke * 0.30), WHITE)

    if with_ticks:
        tr = r - int(stroke * 1.05)
        for ang in (0, 90, 180):  # 3 点 / 6 点 / 9 点
            tx = cx + int(tr * math.cos(math.radians(ang)))
            ty = cy + int(tr * math.sin(math.radians(ang)))
            dot(tx, ty, int(stroke * 0.20), (255, 255, 255, 150))


def build_icon(small=False):
    """small=True 时输出加粗简化版（用于 16/24/32 小尺寸）。"""
    if small:
        img = rounded_gradient(radius=64 * SS)
        draw_glyph(img, 128 * SS, 130 * SS, 64 * SS, 22 * SS, with_ticks=False)
    else:
        img = rounded_gradient(radius=58 * SS)
        draw_glyph(img, 128 * SS, 132 * SS, 62 * SS, 17 * SS, with_ticks=True)
    return img


def ico_file(path, entries):
    """手工组装 ICO（全部 PNG 压缩条目，Vista+ 均支持）。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    dir_entries = b""
    datas = b""
    offset = 6 + 16 * len(entries)
    for size, im in entries:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        data = buf.getvalue()
        w = 0 if size >= 256 else size  # 0 表示 256
        dir_entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32,
                                   len(data), offset)
        datas += data
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + dir_entries + datas)


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    big = build_icon(small=False).resize((256, 256), Image.LANCZOS)
    small = build_icon(small=True)
    entries = []
    for size in (256, 128, 64, 48):          # 常规尺寸来自精细版
        entries.append((size, big.resize((size, size), Image.LANCZOS)))
    for size in (32, 24, 16):                # 小尺寸来自加粗简化版
        entries.append((size, small.resize((size, size), Image.LANCZOS)))
    out = os.path.join(base, "app.ico")
    ico_file(out, entries)
    print("saved:", out, "(%d bytes)" % os.path.getsize(out))


if __name__ == "__main__":
    main()

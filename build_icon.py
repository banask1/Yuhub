"""生成 Yuhub 应用图标 app.ico。

图标源：`resources/app_source_alpha.png`（透明底：两条墨鱼），
没有则退回 `resources/app_source.png`（白底，自动抠白）。

做法要点：
  * **等比缩放，绝不拉伸**。插画是"两条鱼"的横幅（约 2.95:1），
    直接 resize 成正方形会把鱼压扁拉长——2026-10-12 用户报的
    "图标被过度拉伸"就是这么来的。做法：按内容裁齐 → 按长边
    等比缩到方形画布内 → 居中留白，多余部分保持**透明**。
  * 白底源图要先做**白底抠透明**，否则任务栏/开始菜单会带一圈
    白框（深色主题下格外明显）；用"从四边洪水填充"而不是
    "把接近白的都变透明"——后者会把鱼身里的高光也抠出洞。
"""

import os
import sys

from PIL import Image

# 尺寸集：Windows 图标实际会用到的所有大小，16/24/32 是任务栏与列表视图
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256)]


def _has_alpha(im):
    """图片是否自带透明通道（有就直接用，不必抠白）。"""
    if im.mode in ("RGBA", "LA"):
        return True
    return im.mode == "P" and "transparency" in im.info


def _white_to_alpha(im, tolerance=18):
    """把与四边相连的白色背景变透明（保留图案内部的白色）。

    用扫描线式的洪水填充（BFS），从四条边所有"接近白"的像素出发，
    只清除**与边缘连通**的白；被图案包住的白（鱼身高光）不会被动到。
    """
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()

    def is_white(p):
        r, g, b, a = p
        if a < 8:
            return True
        return (255 - r) <= tolerance and (255 - g) <= tolerance \
            and (255 - b) <= tolerance

    from collections import deque
    seen = bytearray(w * h)
    q = deque()

    # 四边入队
    for x in range(w):
        for y in (0, h - 1):
            if not seen[y * w + x] and is_white(px[x, y]):
                seen[y * w + x] = 1
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not seen[y * w + x] and is_white(px[x, y]):
                seen[y * w + x] = 1
                q.append((x, y))

    while q:
        x, y = q.popleft()
        px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                if is_white(px[nx, ny]):
                    seen[ny * w + nx] = 1
                    q.append((nx, ny))
    return im


def _fit_square(im, side, margin=0.05):
    """按**原始宽高比**等比缩放，居中放进 side×side 透明画布。

    ⚠️ 绝不能直接 resize 成正方形：源图是 2.95:1 的横幅，硬拉成
    正方形就是"鱼被压扁拉长"的元凶。这里先按内容裁齐，再按**长边**
    等比缩放，四周留 margin 比例的空白，其余区域保持透明。
    """
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    if w <= 0 or h <= 0:
        return Image.new("RGBA", (side, side), (0, 0, 0, 0))
    inner = max(1, int(round(side * (1.0 - 2.0 * margin))))
    k = min(inner / float(w), inner / float(h))
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - nw) // 2, (side - nh) // 2), im)
    return canvas


def _load_source(here):
    """取图标源：优先透明底的 app_source_alpha.png（直接用），
    否则白底的 app_source.png / .jpg（调用方负责抠白）。"""
    for name in ("app_source_alpha.png", "app_source.png",
                 "app_source.jpg"):
        path = os.path.join(here, "resources", name)
        if os.path.isfile(path):
            im = Image.open(path)
            if _has_alpha(im):
                return im.convert("RGBA"), "alpha"
            return im, "white"
    return None, None


def make_icon(path):
    here = os.path.dirname(os.path.abspath(__file__))
    src, kind = _load_source(here)
    if src is None:
        print("!! 找不到图标源图 resources/app_source_alpha.png",
              file=sys.stderr)
        print("   请把插画另存为该路径后重试；本次保留原有 app.ico。",
              file=sys.stderr)
        return False

    img = src if kind == "alpha" else _white_to_alpha(src, tolerance=20)
    # 等比缩放进 256 方形画布（透明留白，不拉伸）
    img = _fit_square(img, 256, margin=0.05)

    img.save(path, format="ICO", sizes=SIZES)
    print(f"icon saved -> {path}  (源图 {src.size[0]}x{src.size[1]}, {kind})")
    return True


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
    os.makedirs(out_dir, exist_ok=True)
    ok = make_icon(os.path.join(out_dir, "app.ico"))
    sys.exit(0 if ok else 1)

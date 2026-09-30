"""生成 Yuhub 应用图标 app.ico。

图标源：`resources/app_source.png`（锦鲤 + 祥云，蓝紫粉渐变）。

做法要点：
  * 源图直接缩放成多尺寸 ICO。**不要自己再画圆角遮罩** ——
    源图本身就是"圆角方形放在白底上"，抠掉白底即可；
    原逻辑那套"渐变底 + Y 字母"在换成插画后会显得脏。
  * 源图是白底不是透明底，所以要先做**白底抠透明**，
    否则任务栏/开始菜单的图标会带一圈白框（深色主题下格外明显）。
  * 抠底用"从四角洪水填充"而不是"把接近白的都变透明"：
    后者会把锦鲤身体里的白色高光也一起抠掉，图案会出现空洞。
"""

import os
import sys

from PIL import Image

# 尺寸集：Windows 图标实际会用到的大小，16/24/32 是任务栏与列表视图
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256)]


def _white_to_alpha(im, tolerance=18):
    """把与四角相连的白色背景变透明（保留图案内部的白色）。

    用扫描线式的洪水填充（BFS），从四条边所有"接近白"的像素出发，
    只清除**与边缘连通**的白；被图案包住的白（锦鲤的高光）不会被动到。
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


def _trim_to_content(im, pad_ratio=0.0):
    """按不透明区域裁掉多余空白，让图案在图标里占满。"""
    bbox = im.getbbox()
    if not bbox:
        return im
    im = im.crop(bbox)
    if pad_ratio > 0:
        w, h = im.size
        side = max(w, h)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
        im = canvas
    return im


def _load_source(here):
    """找图标源图。优先 resources/app_source.png，其次项目根目录的 jpg。"""
    cands = [
        os.path.join(here, "resources", "app_source.png"),
        os.path.join(here, "resources", "app_source.jpg"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return Image.open(c)
    return None


def make_icon(path):
    here = os.path.dirname(os.path.abspath(__file__))
    src = _load_source(here)
    if src is None:
        print("!! 找不到图标源图 resources/app_source.png", file=sys.stderr)
        print("   请把插画另存为该路径后重试；本次保留原有 app.ico。",
              file=sys.stderr)
        return False

    img = _white_to_alpha(src, tolerance=20)
    img = _trim_to_content(img, pad_ratio=0.0)

    # 缩放到 256（ICO 最大尺寸），用 LANCZOS 保证小尺寸下线条不糊
    img = img.resize((256, 256), Image.LANCZOS)

    img.save(path, format="ICO", sizes=SIZES)
    print(f"icon saved -> {path}  (源图 {src.size[0]}x{src.size[1]})")
    return True


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
    os.makedirs(out_dir, exist_ok=True)
    ok = make_icon(os.path.join(out_dir, "app.ico"))
    sys.exit(0 if ok else 1)

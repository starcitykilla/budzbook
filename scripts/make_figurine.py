#!/usr/bin/env python3
"""Cut a square avatar portrait into a transparent-background figurine asset.

Pipeline (stdlib + Pillow only, no numpy):
  1. Flood-fill the background from the image edges through dark pixels
     (luminance < --tol). Works for dark backdrops (e.g. space scenes):
     the character is brighter and enclosed, so the flood cannot reach it.
  2. Keep the largest connected foreground component (drops stars, glow
     specks and other disconnected bright bits).
  3. Smooth the mask, apply it as the alpha channel -> RGBA cutout.
  4. Trace the silhouette with marching squares, simplify the polygon
     with Douglas-Peucker to a few hundred points.

Outputs in OUT_DIR (default <project>/media/figurines):
    <name>.png       RGBA cutout, transparent background
    <name>.json      {"size": S, "points": [[x, y], ...]} silhouette polygon
                     in image coords, y pointing down, loop auto-closes
    <name>_mask.png  grayscale debug mask for visual verification

Usage:
    python scripts/make_figurine.py media/avatars/Krzybudz.jpg
    python scripts/make_figurine.py fullbody.png --name Krzybudz --bg light
    python scripts/make_figurine.py avatar.png --name shida --out media/figurines --tol 80
"""

import argparse
import json
import math
import os
import sys
from collections import deque

from PIL import Image, ImageFilter

MASK_SIZE = 256          # working resolution for mask + contour tracing
OUT_SIZE = 512           # output PNG resolution
BLUR_RADIUS = 2.0        # mask edge smoothing (px at OUT_SIZE)


# --------------------------------------------------------------------------
# mask
# --------------------------------------------------------------------------

def _luminance(px):
    r, g, b = px[0], px[1], px[2]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def flood_background(img, tol, bg="dark"):
    """Return a bytearray mask (1 = background) flood-filled from the edges.

    bg="dark":  flood through pixels darker than tol (dark backdrops).
    bg="light": flood through pixels brighter than tol (white backdrops).
    """
    w, h = img.size
    px = img.load()

    def is_bg(x, y):
        lum = _luminance(px[x, y])
        return lum < tol if bg == "dark" else lum > tol

    bgm = bytearray(w * h)
    stack = deque()

    def push(x, y):
        i = y * w + x
        if not bgm[i] and is_bg(x, y):
            bgm[i] = 1
            stack.append(i)

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)
    while stack:
        i = stack.pop()
        x = i % w
        y = i // w
        if x > 0:
            push(x - 1, y)
        if x < w - 1:
            push(x + 1, y)
        if y > 0:
            push(x, y - 1)
        if y < h - 1:
            push(x, y + 1)
    return bgm


def largest_component(fg, w, h):
    """Keep only the largest 4-connected foreground component."""
    seen = bytearray(w * h)
    best = []
    for i in range(w * h):
        if fg[i] and not seen[i]:
            comp = []
            stack = deque([i])
            seen[i] = 1
            while stack:
                j = stack.pop()
                comp.append(j)
                x = j % w
                y = j // w
                if x > 0:
                    k = j - 1
                    if fg[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
                if x < w - 1:
                    k = j + 1
                    if fg[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
                if y > 0:
                    k = j - w
                    if fg[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
                if y < h - 1:
                    k = j + w
                    if fg[k] and not seen[k]:
                        seen[k] = 1
                        stack.append(k)
            if len(comp) > len(best):
                best = comp
    mask = bytearray(w * h)
    for j in best:
        mask[j] = 1
    return mask


def build_mask(img, tol, bg="dark", close=11):
    """Full mask pipeline. Returns (mask_bytearray, w, h) at MASK_SIZE.

    close: morphological closing kernel (px) applied to the foreground
    before keeping the largest component; bridges narrow background gaps
    (e.g. dark recesses in a bright crown) so bright accessories stay
    connected to the main silhouette. 0 disables.
    """
    small = img.resize((MASK_SIZE, MASK_SIZE), Image.LANCZOS).convert("RGB")
    w, h = small.size
    bgm = flood_background(small, tol, bg)
    n_bg = sum(bgm)
    frac = n_bg / (w * h)
    if frac < 0.02 or frac > 0.98:
        raise ValueError(
            "background detection failed (bg fraction %.2f); try --tol/--bg" % frac)
    fg = bytearray(1 - b for b in bgm)
    if close:
        fg_img = Image.new("L", (w, h))
        fg_img.putdata(bytes(255 * v for v in fg))
        fg_img = fg_img.filter(ImageFilter.MaxFilter(close))
        fg_img = fg_img.filter(ImageFilter.MinFilter(close))
        fg = bytearray(1 if v > 127 else 0 for v in fg_img.getdata())
    return largest_component(fg, w, h), w, h


# --------------------------------------------------------------------------
# contour tracing (marching squares + loop chaining)
# --------------------------------------------------------------------------

def _ms_segments(mask, w, h):
    """Yield (p1, p2) segments of the iso-contour at the 0.5 level."""
    segs = []
    for y in range(h - 1):
        row = y * w
        for x in range(w - 1):
            tl = mask[row + x]
            tr = mask[row + x + 1]
            br = mask[row + x + 1 + w]
            bl = mask[row + x + w]
            idx = (tl << 3) | (tr << 2) | (br << 1) | bl
            if idx == 0 or idx == 15:
                continue
            T = (x + 0.5, y)
            R = (x + 1.0, y + 0.5)
            B = (x + 0.5, y + 1.0)
            L = (x, y + 0.5)
            if idx == 1:
                segs.append((L, B))
            elif idx == 2:
                segs.append((B, R))
            elif idx == 3:
                segs.append((L, R))
            elif idx == 4:
                segs.append((T, R))
            elif idx == 5:      # ambiguous saddle: keep fg corners separate
                segs.append((L, T))
                segs.append((B, R))
            elif idx == 6:
                segs.append((T, B))
            elif idx == 7:
                segs.append((L, T))
            elif idx == 8:
                segs.append((L, T))
            elif idx == 9:
                segs.append((T, B))
            elif idx == 10:     # ambiguous saddle
                segs.append((T, R))
                segs.append((L, B))
            elif idx == 11:
                segs.append((T, R))
            elif idx == 12:
                segs.append((L, R))
            elif idx == 13:
                segs.append((R, B))
            elif idx == 14:
                segs.append((B, L))
    return segs


def _chain_loops(segs):
    """Chain segments into closed loops. Returns list of point lists."""
    adj = {}
    for a, b in segs:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    used = set()
    loops = []
    for (a, b) in segs:
        if (a, b) in used or (b, a) in used:
            continue
        loop = [a]
        prev, cur = a, b
        used.add((a, b))
        while cur != a:
            loop.append(cur)
            nxt = None
            for nb in adj[cur]:
                if (cur, nb) not in used and (nb, cur) not in used and nb != prev:
                    nxt = nb
                    break
            if nxt is None:
                break  # open chain; shouldn't happen for closed masks
            used.add((cur, nxt))
            prev, cur = cur, nxt
        if cur == a and len(loop) > 8:
            loops.append(loop)
    return loops


def _pad_mask(mask, w, h):
    """Add a 1px background border so edge-touching components trace cleanly.
    Returns (padded_mask, w+2, h+2)."""
    pw, ph = w + 2, h + 2
    padded = bytearray(pw * ph)
    for y in range(h):
        row_src = y * w
        row_dst = (y + 1) * pw + 1
        padded[row_dst:row_dst + w] = mask[row_src:row_src + w]
    return padded, pw, ph


def trace_polygon(mask, w, h):
    """Longest contour loop of the mask, as [(x, y), ...] in mask coords."""
    padded, pw, ph = _pad_mask(mask, w, h)
    loops = _chain_loops(_ms_segments(padded, pw, ph))
    if not loops:
        raise ValueError("no contour found in mask")
    # undo the padding offset
    return [(x - 1.0, y - 1.0) for x, y in max(loops, key=len)]


# --------------------------------------------------------------------------
# simplification (Douglas-Peucker, closed loop)
# --------------------------------------------------------------------------

def _perp_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def douglas_peucker(pts, eps):
    """Simplify an open point list; returns index-kept subset."""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        dmax, imax = 0.0, -1
        for i in range(s + 1, e):
            d = _perp_dist(pts[i], pts[s], pts[e])
            if d > dmax:
                dmax, imax = d, i
        if dmax > eps:
            keep[imax] = True
            stack.append((s, imax))
            stack.append((imax, e))
    return [p for p, k in zip(pts, keep) if k]


def simplify_closed(loop, target_min=100, target_max=300):
    """Adapt epsilon so the closed loop lands in the target point range."""
    pts = loop + [loop[0]]  # close it for DP
    eps = 0.25
    for _ in range(24):
        simp = douglas_peucker(pts, eps)
        # drop the duplicated closing point for counting
        n = len(simp) - 1
        if n > target_max:
            eps *= 1.6
        elif n < target_min:
            eps /= 1.6
            if eps < 0.05:
                break
        else:
            break
    simp = douglas_peucker(pts, eps)
    if simp[0] == simp[-1]:
        simp = simp[:-1]
    return simp


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _pad_to_square(img, bg="dark"):
    """Pad a non-square image to square using the corner-sampled bg color."""
    w, h = img.size
    if w == h:
        return img
    side = max(w, h)
    corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)),
               img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
    fill = tuple(int(sum(c[i] for c in corners) / 4) for i in range(3))
    sq = Image.new("RGB", (side, side), fill)
    sq.paste(img, ((side - w) // 2, (side - h) // 2))
    return sq


def make_figurine(src_path, name, out_dir, tol=70.0, bg="dark", close=11):
    img = Image.open(src_path).convert("RGB")
    img = _pad_to_square(img, bg)
    img = img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)

    mask, mw, mh = build_mask(img, tol, bg, close)
    fg_frac = sum(mask) / (mw * mh)
    print("foreground fraction: %.3f" % fg_frac, file=sys.stderr)

    # debug mask
    mask_img = Image.new("L", (mw, mh))
    mask_img.putdata(bytes(255 * v for v in mask))

    # smooth alpha at output size
    alpha = mask_img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
    alpha = alpha.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))

    out = img.copy()
    out.putalpha(alpha)

    # contour in output coords
    loop = trace_polygon(mask, mw, mh)
    simp = simplify_closed(loop)
    scale = OUT_SIZE / mw
    points = [[round(x * scale, 1), round(y * scale, 1)] for x, y in simp]
    print("polygon points: %d" % len(points), file=sys.stderr)

    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, name + ".png")
    json_path = os.path.join(out_dir, name + ".json")
    mask_path = os.path.join(out_dir, name + "_mask.png")
    out.save(png_path, "PNG")
    with open(json_path, "w") as f:
        json.dump({"size": [OUT_SIZE, OUT_SIZE], "points": points}, f)
    mask_img.save(mask_path, "PNG")
    return {"png": png_path, "json": json_path, "mask": mask_path,
            "points": len(points), "fg_frac": fg_frac}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="square avatar image (JPG/PNG)")
    ap.add_argument("--name", default=None, help="output base name")
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--tol", type=float, default=None,
                    help="background luminance threshold "
                         "(default 70 for dark bg, 200 for light bg)")
    ap.add_argument("--bg", choices=["dark", "light"], default="dark",
                    help="background type: dark backdrop (default) or white/light")
    ap.add_argument("--close", type=int, default=11,
                    help="morphological closing kernel px (default 11, 0 disables)")
    args = ap.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    name = args.name or os.path.splitext(os.path.basename(args.src))[0]
    out_dir = args.out or os.path.join(root, "media", "figurines")
    tol = args.tol if args.tol is not None else (200.0 if args.bg == "light" else 70.0)
    res = make_figurine(args.src, name, out_dir, tol=tol, bg=args.bg, close=args.close)
    print(json.dumps({k: v for k, v in res.items() if k != "points"},
                     indent=1))
    print("points: %d  fg_frac: %.3f" % (res["points"], res["fg_frac"]))


if __name__ == "__main__":
    main()

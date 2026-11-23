"""Step 3 of 3 - the player's binaries from the Sega CD bitmaps:
  python scd_pack.py BD04|ALL [unit names...] [--force] [--no-verify] [--workers N]
      -> out/set/<unit>.bin (one stream per unit, format version 5, see encode_v3.py) + out/set_BD04.json
  python scd_pack.py MENUS       -> out/set/SAVEMENU.bin (save menu) + out/menus/PAUSE.SPR (pause overlay)
  python scd_pack.py CHEAT       -> out/set/CHEATMENU.bin + out/menus/CHEAT.SPR, cheat.json (the cheat menu)
  then:  python build_disc.py out/set_BD04.json bd04

Reads only step 2's folders (scd/<stage>/<unit>/, see scd_format.py), edited or not.  No colour decisions are
made here: each 8x8 cell's indices are split into the plane B tile (the cell's most used palette line) and, for a
second line, the plane A tile on top; the shadow bit sets the cell's priority.  A frame lists the cells that differ
from its scene's background; a cell whose new pixels all sit on one line on top of the background's plane B only
sends a plane A tile.  Pixels that break the hardware rules (a third palette line in a cell, mixed normal and
shadowed pixels, indices 128-255) are moved to the nearest allowed colour and reported, as are tile budgets that
force a lossy fallback.  Cel layers may be indexed (exact) or true colour (magenta = transparent; colours that
sit on several palette lines are resolved per cell, which can cost tile sharing).  --verify (default) decodes every stream as the Sega CD would and compares it with the
bitmaps.  A unit is rebuilt when any of its files is newer than its stream, or with --force.
"""
import json, os, sys, time
from multiprocessing import Pool
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SCD = os.path.join(HERE, 'scd')
OUTDIR = os.path.join(HERE, 'out', 'set')


class Pal:
    """A scene's palette: grid codes, CRAM, and per bitmap index (0-127) its colour as shown (OKLab, RGB)."""
    def __init__(self, bmp_pal):
        import encode_poc as E, scd_format as F
        self.codes = F.codes_of(bmp_pal)
        self.cram = [0] * 64
        self.rgb = np.zeros((256, 3))
        for l in range(4):
            for c in range(1, 16):
                code = int(self.codes[l][c - 1])
                self.cram[l * 16 + c] = E.cram_word(code)
                self.rgb[l * 16 + c], self.rgb[64 + l * 16 + c] = E.GN[code], E.GS[code]
        self.rgb[128:] = [bmp_pal[i] for i in range(128, 256)]
        self.lab = E.oklab(self.rgb).astype(np.float32)
        # nearest colour 1-15 of each line, shown in each mode, for every index: near[idx, mode, line]
        d = ((self.lab[:, None] - self.lab[None, :128]) ** 2).sum(2)          # (256,128)
        self.near = np.zeros((256, 2, 4), np.uint8)
        for s in (0, 1):
            for l in range(4):
                cand = s * 64 + l * 16 + np.arange(1, 16)
                self.near[:, s, l] = d[:, cand].argmin(1) + 1
        self.dist = d


def canon(idx):
    """Colour 0 of any line is the same black: one index per mode."""
    return np.where(idx % 16 == 0, idx & 64, idx)


def legalise(cells, pal, report):
    """cells (n,64) bitmap indices -> legal indices (one shadow bit and at most two lines per cell)."""
    out = cells.astype(np.int16).copy()
    bad = out >= 128
    if bad.any():
        report['bad index'] = report.get('bad index', 0) + int(bad.sum())
        out[bad] = pal.dist[out[bad]].argmin(1)
    sh = (out >= 64)
    s = (sh.sum(1) * 2 > 64).astype(int)                       # majority; ties normal
    for i in np.where((sh != s[:, None]).any(1))[0]:
        m = sh[i] != s[i]
        cand = s[i] * 64 + np.arange(64)
        new = cand[pal.dist[out[i, m]][:, cand].argmin(1)]
        changed = (pal.rgb[new] != pal.rgb[out[i, m]]).any(1)
        if changed.any():
            report['mixed shadow'] = report.get('mixed shadow', 0) + int(changed.sum())
        out[i, m] = new
    c, l = out % 16, (out % 64) // 16
    cnt = np.stack([((l == k) & (c > 0)).sum(1) for k in range(4)], 1)
    for i in np.where((cnt > 0).sum(1) > 2)[0]:
        keep = np.argsort(-cnt[i], kind='stable')[:2]
        m = (c[i] > 0) & ~np.isin(l[i], keep)
        cand = np.concatenate([s[i] * 64 + k * 16 + np.arange(16) for k in keep])
        new = cand[pal.dist[out[i, m]][:, cand].argmin(1)]
        changed = (pal.rgb[new] != pal.rgb[out[i, m]]).any(1)
        if changed.any():
            report['third line'] = report.get('third line', 0) + int(changed.sum())
        out[i, m] = new
    return out.astype(np.uint8), s


def layers(idx, s, pal):
    """One legal cell -> (B line, B pixels, A line or -1, A pixels)."""
    c, l = idx % 16, (idx % 64) // 16
    used = l[c > 0]
    if not len(used):
        return 0, np.zeros(64, np.uint8), -1, None
    cnt = np.bincount(used, minlength=4)
    order = np.argsort(-cnt, kind='stable')
    bl = int(order[0])
    if cnt[order[1]] == 0:
        return bl, c.astype(np.uint8), -1, None
    al = int(order[1])
    hole = (l == al) & (c > 0)
    bpx = np.where((l == bl) & (c > 0), c, 0).astype(np.uint8)
    bpx[hole] = pal.near[idx[hole], s, bl]                      # under plane A: nearest colour of B's line
    return bl, bpx, al, np.where(hole, c, 0).astype(np.uint8)


def collapse(idx, s, pal):
    """A two-line cell as one line (its plane B line): what it shows then."""
    bl, bpx, al, apx = layers(idx, s, pal)
    return (s * 64 + bl * 16 + bpx).astype(np.uint8)


def base_maps(cells, sm, pal, budget, add, report, what):
    """Scene background cells (n,64 legal) -> map B, map A words and plane B as shown (n,64), within budget tiles
    (add(px) -> map bits; returns the tile count via add.count())."""
    import encode_v3 as V
    cells = cells.copy()
    while True:
        add.reset()
        lay = [layers(cells[i], sm[i], pal) for i in range(len(cells))]
        prio = np.where(sm == 0, 0x8000, 0)
        bB = [None] * len(lay)
        for i, (bl, bpx, al, apx) in enumerate(lay):              # one-layer cells first ...
            if al < 0:
                bB[i] = add(bpx)
        for i, (bl, bpx, al, apx) in enumerate(lay):              # ... then plane B under a plane A: its hidden
            if al >= 0:                                           # pixels may be anything, so reuse a tile that
                m = add.match(bpx, apx == 0)                      # matches the visible ones when there is one
                if m is not None:
                    lay[i] = (bl, m, al, apx)
                bB[i] = add(lay[i][1])
        mB = [int(prio[i] | (lay[i][0] << 13) | bB[i]) for i in range(len(lay))]
        mA = [int(prio[i] | (al << 13) | add(apx)) if al >= 0 and apx.any() else 0
              for i, (bl, bpx, al, apx) in enumerate(lay)]
        over = add.count() - budget
        if over <= 0:
            break
        two = [i for i, (bl, bpx, al, apx) in enumerate(lay) if al >= 0]
        if not two:
            raise ValueError(f'{what}: {add.count()} tiles even with one layer per cell (budget {budget})')
        two.sort(key=lambda i: int((lay[i][3] > 0).sum()))
        for i in two[:max(over, 1)]:
            cells[i] = collapse(cells[i], sm[i], pal)
        report['second layer dropped (tile budget)'] = report.get('second layer dropped (tile budget)', 0) + min(over, len(two))
    Bshow = np.array([sm[i] * 64 + bl * 16 + bpx for i, (bl, bpx, al, apx) in enumerate(lay)], np.uint8)
    return mB, mA, Bshow, lay, cells


class SceneTiles:
    """Scene tile store with flip-aware de-duplication (as encode_v3.TilePool), map bits = flips | VRAM tile."""
    def __init__(self, first=1, cap=None):
        self.first, self.cap = first, cap
        self.reset()

    def reset(self):
        self.tiles, self.seen, self.px = [], {}, []

    def match(self, px, care):
        """A stored tile (or flip of one) equal to px (64,) where care (64,) is set, as 64 pixels; else None."""
        if not self.px:
            return None
        P = np.array(self.px).reshape(-1, 8, 8)
        V4 = np.stack([P, P[:, :, ::-1], P[:, ::-1], P[:, ::-1, ::-1]], 1).reshape(-1, 64)
        ok = ((V4 == px) | ~care).all(1)
        return V4[np.argmax(ok)].astype(np.uint8) if ok.any() else None

    def __call__(self, px):
        import encode_poc as E, encode_v3 as V
        px = np.asarray(px).reshape(8, 8)
        for v, fl in ((px, 0), (px[:, ::-1], 0x800), (px[::-1], 0x1000), (px[::-1, ::-1], 0x1800)):
            if v.tobytes() in self.seen:
                return fl | V.vram_tile(self.first + self.seen[v.tobytes()])
        self.seen[px.tobytes()] = len(self.tiles)
        self.px.append(px.reshape(-1).copy())
        self.tiles.append(E.pack_tile(px.reshape(-1)))
        k = len(self.tiles) - 1 if self.cap is None else min(len(self.tiles) - 1, self.cap - 1)
        return V.vram_tile(self.first + k)

    def count(self):
        return len(self.tiles)


def frame_cells(fc, fs, bgc, Bshow, bsm, widx, pal, report):
    """The listed cells of a frame: fc (n,64) legal frame cells that differ from the background, fs their shadow
    bits, bgc/Bshow/bsm the background cells, its plane B as shown and its modes; widx map word indices.
    Returns entries, mode words, tile slots and what the cells show (n,64: fc, unless the pool forced merging)."""
    import encode_poc as E, encode_v3 as V
    n = len(fc)
    entries, tl = [], []
    bits = np.zeros((n + 15) // 16, int)
    for i in range(n):
        e = int(widx[i])
        diff = canon(fc[i]) != canon(Bshow[i])
        c, l = fc[i] % 16, (fc[i] % 64) // 16
        if fs[i] == bsm[i] and (c[diff] > 0).all() and len(set(l[diff].tolist())) <= 1:
            if diff.any():                                       # new pixels on one line over the background's B
                tl.append(E.pack_tile(np.where(diff, c, 0))); e |= 0x1000 | (int(l[diff][0]) << 10)
        else:
            bl, bpx, al, apx = layers(fc[i], fs[i], pal)
            tl.append(E.pack_tile(bpx)); e |= 0x8000 | (bl << 13)
            if fs[i] == 0:
                bits[i // 16] |= 1 << (i % 16)
            if al >= 0 and apx.any():
                tl.append(E.pack_tile(apx)); e |= 0x1000 | (al << 10)
        entries.append(e)
    shown = fc
    if len(set(tl)) > V.POOLMAX:
        # more different tiles than the frame pool holds: one plane B tile per cell, the most similar merged
        report['frames merged (pool)'] = report.get('frames merged (pool)', 0) + 1
        single = np.array([collapse(fc[i], fs[i], pal) for i in range(n)])
        rep = V.merge_to_budget(pal.lab[single], V.POOLMAX)
        entries, tl = [], []
        bits = np.zeros((n + 15) // 16, int)
        for i in range(n):
            bl, bpx, _, _ = layers(single[rep[i]], fs[rep[i]], pal)
            entries.append(int(widx[i]) | 0x8000 | (bl << 13)); tl.append(E.pack_tile(bpx))
            if fs[rep[i]] == 0:
                bits[i // 16] |= 1 << (i % 16)
        shown = single[rep]
    return entries, [int(b) for b in bits], tl, shown


def untile(t, rows):
    return t.reshape(rows, -1, 8, 8).swapaxes(1, 2).reshape(rows * 8, -1)


def pack_static(bg, frames, bp, P, report, what='background'):
    """A static scene from bitmap indices: bg (192,224), frames [(192,224)], bp the palette.  Returns the
    records (scene + frames, audio left empty), and what the background and each frame show (indices)."""
    import struct
    import encode_poc as E, encode_v3 as V
    pal = Pal(bp)
    V.set_layout(P)
    bgc, bsm = legalise(E.tiles(bg), pal, report)
    add = SceneTiles(cap=V.SCENE_MAX)
    mB, mA, Bshow, _, bgc = base_maps(bgc, bsm, pal, V.SCENE_MAX, add, report, what)
    rec = bytearray(b''.join(struct.pack('>H', w) for w in pal.cram))
    rec += struct.pack('>HH', len(add.tiles), P)
    rec += b''.join(struct.pack('>H', w) for w in mB) + b''.join(struct.pack('>H', w) for w in mA)
    rec += b''.join(add.tiles)
    recs, prev_pool, shown = [V.record(1, rec)], None, []
    for j, fi in enumerate(frames):
        fc, fs = legalise(E.tiles(fi), pal, report)
        ch = np.where((canon(fc) != canon(bgc)).any(1) | (fs != bsm))[0]
        ent, bits, tl, sh = frame_cells(fc[ch], fs[ch], bgc[ch], Bshow[ch], bsm[ch], V.CELL_IDX[ch], pal, report)
        body, prev_pool = V.frame_body(bytes(E.AUD_PER_FRAME), ent, bits, tl, prev_pool=prev_pool if j else None)
        recs.append(V.record(2, body))
        if j == 0 and len(rec) + len(body) >= 150000:
            raise ValueError(f'{what}: scene + first frame over the 150 KB look-ahead')
        cells = bgc.copy(); cells[ch] = sh
        shown.append(untile(cells, V.TH))
    return recs, untile(bgc, V.TH), shown


def pack_scroll(virt, cam, frames, bp, report, what='background'):
    """A scrolling scene from bitmap indices: virt (Hg,224) the virtual background, cam the camera line of each
    frame, frames [(192,224) windows].  Returns records, and what the background and each window show."""
    import struct
    import encode_poc as E, encode_v3 as V
    V.set_layout(410)
    pal = Pal(bp)
    Hg = len(virt)
    K = (16 - cam[0]) % 256
    mask = next(((l << 8) | (j + 1) for l in range(4) for j in range(15)
                 if pal.codes[l][j] == 0 and not (l == 3 and j + 1 >= 14)), None)
    if mask is None:
        raise ValueError(f'{what}: the palette needs a black colour (border sprites), not line 3 14/15')
    rows_of = lambda b: virt[np.clip(np.arange(8 * b - K, 8 * b - K + 8), 0, Hg - 1)]
    bands = range(K // 8, (Hg - 1 + K) // 8 + 1)
    enc, vshown = {}, virt.copy()
    for b in bands:
        cells, sm = legalise(E.tiles(rows_of(b)), pal, report)
        add = SceneTiles(first=(b % 32) * V.ROWK + 1, cap=V.ROWK)
        mB, mA, Bshow, _, cells = base_maps(cells, sm, pal, V.ROWK, add, report, f'{what} band {b}')
        enc[b] = (add.tiles, mB, mA, cells, sm, Bshow)
        v = np.arange(8 * b - K, 8 * b - K + 8); ok = (v >= 0) & (v < Hg)
        vshown[v[ok]] = untile(cells, 1)[ok]
    needed = lambda c: range((c + K) // 8, (c + E.WIN_H - 1 + K) // 8 + 1)
    ring = {}
    mapB, mapA = np.zeros((32, V.TW), int), np.zeros((32, V.TW), int)
    tiles = [bytes(32)] * (32 * V.ROWK)
    for b in needed(cam[0]):
        m = b % 32
        t, mb, ma = enc[b][:3]
        mapB[m], mapA[m] = mb, ma
        tiles[m * V.ROWK:m * V.ROWK + len(t)] = t
        ring[m] = b
    rec = bytearray(b''.join(struct.pack('>H', w) for w in pal.cram))
    rec += struct.pack('>HHHH', len(tiles), mask, (cam[0] + K - 16) % 256, V.POOLMAX)
    rec += b''.join(struct.pack('>H', int(w)) for w in mapB.reshape(-1))
    rec += b''.join(struct.pack('>H', int(w)) for w in mapA.reshape(-1))
    rec += b''.join(tiles)
    recs, prev_pool, shown = [V.record(5, rec)], None, []
    for i, win in enumerate(frames):
        c = cam[i]
        rows = []
        for b in needed(c):
            m = b % 32
            if ring.get(m) != b:
                t, mb, ma = enc[b][:3]
                rows.append((m, mb, ma, t)); ring[m] = b
        fcs, fss, bgcs, Bss, bsms, widx, where = [], [], [], [], [], [], []
        for b in needed(c):
            r = np.arange(8 * b - K, 8 * b - K + 8) - c
            inwin = (r >= 0) & (r < E.WIN_H)
            # rows outside the window are hidden by the border sprites: they repeat the cell's visible rows (so
            # the cell stays one mode / two lines) and do not count as changes
            plane = win[np.clip(r, 0, E.WIN_H - 1)]
            fc, fs = legalise(E.tiles(plane), pal, report)
            vis = E.tiles(np.repeat(inwin[:, None], E.WIN_W, 1)[:, :, None])[:, :, 0]
            _, _, _, cells, sm, Bshow = enc[b]
            ch = np.where(((canon(fc) != canon(cells)) & vis).any(1) | (fs != sm))[0]
            fcs += list(fc[ch]); fss += list(fs[ch]); bgcs += list(cells[ch]); Bss += list(Bshow[ch])
            bsms += list(sm[ch]); widx += [(b % 32) * 32 + col + 2 for col in ch]; where += [(b, col) for col in ch]
        img = vshown[c:c + E.WIN_H].copy()
        if fcs:
            ent, bits, tl, sh = frame_cells(np.array(fcs), np.array(fss), np.array(bgcs), np.array(Bss),
                                            np.array(bsms), widx, pal, report)
            for (b, col), px in zip(where, sh):
                r = np.arange(8 * b - K, 8 * b - K + 8) - c
                ok = (r >= 0) & (r < E.WIN_H)
                img[r[ok], col * 8:col * 8 + 8] = px.reshape(8, 8)[ok]
        else:
            ent, bits, tl = [], [], []
        body, prev_pool = V.frame_body(bytes(E.AUD_PER_FRAME), ent, bits, tl, (c + K - 16) % 256, rows,
                                       prev_pool=None if i == 0 else prev_pool)
        recs.append(V.record(2, body))
        if i == 0 and len(rec) + len(body) >= 150000:
            raise ValueError(f'{what}: scene + first frame over the 150 KB look-ahead')
        shown.append(img)
    return recs, vshown, shown


def cel_from_rgb(rgb, under, pal, memory=None):
    """A cel layer saved as true colour (h,w,3) over the background indices under (h,w) -> indices.  Magenta is
    transparent.  A colour can sit at several indices (same colour on two lines, a shadowed colour equal to a
    normal one), so each 8x8 cell picks consistently: the shadow half most of its pixels fit (ties: the
    background's), then lines the cell already uses; colours not in the palette take the nearest entry."""
    import encode_poc as E
    h, w = under.shape
    out = np.zeros((h, w), np.uint8)
    prgb = pal.rgb[:128].astype(int)
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            px = rgb[y:y + 8, x:x + 8].reshape(-1, 3).astype(int)
            bg = under[y:y + 8, x:x + 8].reshape(-1)
            opaque = ~(px == (255, 0, 255)).all(1)
            if not opaque.any():
                continue
            eq = (prgb[None] == px[:, None]).all(2)               # (64,128) exact matches
            eq[:, np.arange(128) % 16 == 0] = False               # black: any line
            black = ~px.any(1)
            vis = bg[~opaque]                                     # background pixels still showing
            bgs = int((bg >= 64).sum() * 2 > 64)
            vlines = set(((vis % 64) // 16)[vis % 16 > 0].tolist())
            bgl = np.bincount(((bg % 64) // 16)[bg % 16 > 0], minlength=4)    # the background cell's lines
            prev = memory.get((y, x)) if memory is not None else None
            best = None
            for s in (0, 1):
                if len(vis) and s != (vis[0] >= 64):
                    continue                                      # the cell's visible background fixes its mode
                for pair in [(a, b) for a in range(4) for b in range(a, 4)]:
                    if not vlines <= set(pair):
                        continue
                    ok = np.zeros(128, bool)
                    for l in pair:
                        ok[s * 64 + l * 16 + 1:s * 64 + l * 16 + 16] = True
                    score = int(((eq & ok).any(1) | black)[opaque].sum())
                    # ties (the same colours on several lines): the previous frame's choice here, the background's
                    # mode and lines (keeps tiles shared with earlier frames and the background)
                    key = (score, prev == (s, pair), s == bgs, int(bgl[list(set(pair))].sum()), -len(set(pair)))
                    if best is None or key > best[0]:
                        best = (key, s, pair, ok)
            _, s, pair, ok = best
            if memory is not None:
                memory[(y, x)] = (s, pair)
            cell = np.zeros(64, np.uint8)
            cand_all = np.where(ok)[0]
            for i in np.where(opaque)[0]:
                if black[i]:
                    cell[i] = s * 64 + 16; continue
                ex = np.where(eq[i] & ok)[0]
                if len(ex) > 1:                                   # on both lines: the background's line first
                    ex = sorted(ex, key=lambda k: -bgl[(k % 64) // 16])
                cell[i] = ex[0] if len(ex) else cand_all[((prgb[cand_all] - px[i]) ** 2).sum(1).argmin()]
            out[y:y + 8, x:x + 8] = cell.reshape(8, 8)
    return out


def read_cel(path, under, bp, pal, memory=None):
    """A cel layer bitmap (indexed, or true colour) as indices; under = the background indices beneath it;
    memory: a dict kept across a scene's frames (true-colour choices per cell)."""
    from PIL import Image
    import scd_format as F
    im = Image.open(path)
    if im.mode == 'P':
        return F.read_bmp(path)[0]
    full = np.zeros((im.height, im.width), np.uint8)
    rgb = np.array(im.convert('RGB'))
    full[F.WY:F.WY + under.shape[0], F.WX:F.WX + under.shape[1]] = cel_from_rgb(
        rgb[F.WY:F.WY + under.shape[0], F.WX:F.WX + under.shape[1]], under, pal, memory)
    return full


def pack_wide(img, bp, report, what='menu'):
    """A menu screen for H40 (scd_format: kind 'wide'): img (224,320) indices.  Record type 6: 64 CRAM words | u16
    tiles, u16 0 | map B 40 x 28 words | map A 40 x 28 | tiles (VRAM tiles 1, 2, ...), then one frame record that
    changes nothing.  Returns the records and what the screen shows (indices)."""
    import struct
    import encode_poc as E, encode_v3 as V
    V.set_layout_wide()
    pal = Pal(bp)
    cells, sm = legalise(E.tiles(img), pal, report)
    add = SceneTiles(cap=V.SCENE_MAX)
    mB, mA, Bshow, _, cells = base_maps(cells, sm, pal, V.SCENE_MAX, add, report, what)
    rec = bytearray(b''.join(struct.pack('>H', w) for w in pal.cram))
    rec += struct.pack('>HH', len(add.tiles), 0)
    rec += b''.join(struct.pack('>H', w) for w in mB) + b''.join(struct.pack('>H', w) for w in mA)
    rec += b''.join(add.tiles)
    body, _ = V.frame_body(bytes(E.AUD_PER_FRAME), [], [], [])
    return [V.record(6, rec), V.record(2, body)], untile(cells, img.shape[0] // 8)


def wide_render(stream, pal):
    """Decode a wide screen's record (type 6) back into indices (as the video chip shows them)."""
    import struct
    p = 16
    t, ln = struct.unpack('>HI', stream[p:p + 6]); b = stream[p + 6:p + 6 + ln]
    assert t == 6
    nt = struct.unpack('>H', b[128:130])[0]
    mB = struct.unpack('>1120H', b[132:132 + 2240]); mA = struct.unpack('>1120H', b[2372:2372 + 2240])
    tiles = [np.frombuffer(b[4612 + 32 * k:4644 + 32 * k], np.uint8) for k in range(nt)]
    tile = lambda w: (lambda a: np.stack([a >> 4, a & 15], 1).reshape(8, 8))(tiles[(w & 0x7FF) - 1])
    out = np.zeros((224, 320), np.uint8)
    for i in range(1120):
        def px(w):
            if not (w & 0x7FF):
                return np.zeros((8, 8), np.uint8)
            t_ = tile(w)
            if w & 0x800: t_ = t_[:, ::-1]
            if w & 0x1000: t_ = t_[::-1]
            return t_
        s = 0 if (mB[i] | mA[i]) & 0x8000 else 64
        b_, a_ = px(mB[i]), px(mA[i])
        v = np.where(a_ > 0, s + ((mA[i] >> 13) & 3) * 16 + a_, s + ((mB[i] >> 13) & 3) * 16 + b_)
        out[(i // 40) * 8:(i // 40) * 8 + 8, (i % 40) * 8:(i % 40) * 8 + 8] = v
    return out


def scene_from_files(d, sc, report):
    """Read a scene's bitmaps (background + cel layers) and pack it.  Returns records and the frames as the
    bitmaps show them (RGB, for --verify)."""
    import encode_poc as E, scd_format as F
    bgi, bp = F.read_bmp(os.path.join(d, sc['background']))
    if sc['kind'] == 'wide':                                    # a menu screen (H40): no cel changes
        recs, shown = pack_wide(bgi, bp, report, sc['background'])
        return recs, [shown]
    pal, memory = Pal(bp), {}
    cel = lambda j, under: F.window(read_cel(os.path.join(d, F.cel_file(sc, sc['first'] + j)), under, bp, pal,
                                             memory))
    if sc['kind'] == 'scroll':
        virt = bgi[:, F.WX:F.WX + E.WIN_W]
        frames = [F.compose(virt[c:c + E.WIN_H], cel(j, virt[c:c + E.WIN_H])) for j, c in enumerate(sc['camera'])]
        recs, _, _ = pack_scroll(virt, sc['camera'], frames, bp, report, sc['background'])
    else:
        frames = [F.compose(F.window(bgi), cel(j, F.window(bgi))) for j in range(sc['frames'])]
        recs, _, _ = pack_static(F.window(bgi), frames, bp, sc.get('pool', 410), report, sc['background'])
    return recs, [pal.rgb[f] for f in frames]


def pack_unit(job):
    stage, name, force, verify = job
    import encode_poc as E, encode_v3 as V, scd_format as F
    d = os.path.join(SCD, stage, name)
    out = os.path.join(OUTDIR, name + '.bin')
    newest = max(os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d))
    if not force and os.path.exists(out) and os.path.getmtime(out) > newest:
        return name, None, 'up to date'
    t0 = time.time()
    u = F.load_unit(d)
    report, recs, shows = {}, [], []
    for sc in u['scenes']:
        r, s = scene_from_files(d, sc, report)
        recs += r; shows += s
    audio = F.read_audio(os.path.join(d, u['audio']), u['frames'])
    stream = V.write_stream(V.set_audio(recs, audio), u['frames'], len(u['scenes']), out)
    msg = f'{u["frames"]} frames, {len(stream) / 1024:.0f} KB'
    if verify and u['scenes'][0]['kind'] == 'wide':
        bg = F.read_bmp(os.path.join(d, u['scenes'][0]['background']))[0]
        wrong = int((canon(wide_render(stream, None)) != canon(bg)).sum())
        msg += f', verify: {"exact" if wrong == 0 else f"{wrong} pixels differ from the bitmap"}'
        verify = False
    if verify:
        imgs = V.simulate(stream)
        wrong = sum(int((np.abs(a.astype(int) - b.astype(int)).max(2) > 0).sum()) for a, b in zip(imgs, shows))
        msg += f', verify: {"exact" if wrong == 0 else f"{wrong} pixels differ from the bitmaps"}'
    if report:
        msg += ', repaired: ' + ', '.join(f'{k} {v}' for k, v in report.items())
    return name, len(stream), msg + f' ({time.time() - t0:.0f} s)'


def pause_sprite(path, out):
    """scd/MENUS/pause.bmp (index 1 fill, 2 outline) -> PAUSE.SPR for app.s: u16 tiles per layer, u16 sprites per
    layer; per sprite u16 y + 128, size (w-1)<<10 | (h-1)<<8, first tile, x + 128 (screen pixels); then the fill
    layer's tiles and the outline layer's as 1-bit masks (8 bytes each, bit 7 = left pixel).  Sprites are at most
    4 x 4 tiles; a sprite's tiles go down its columns, as the video chip reads them."""
    import struct
    import scd_format as F
    idx, _ = F.read_bmp(path)
    ys, xs = np.nonzero(idx)
    y0, x0 = int(ys.min()), int(xs.min())
    tw, th = (int(xs.max()) - x0) // 8 + 1, (int(ys.max()) - y0) // 8 + 1
    area = np.zeros((th * 8, tw * 8), np.uint8)
    area[:int(ys.max()) - y0 + 1, :int(xs.max()) - x0 + 1] = idx[y0:ys.max() + 1, x0:xs.max() + 1]
    w, h = int(xs.max()) - x0 + 1, int(ys.max()) - y0 + 1          # shown in the middle of the screen
    x0, y0 = (F.SCREEN_W - w) // 2, (F.SCREEN_H - h) // 2
    sprites, tiles = [], []
    for sy in range(0, th, 4):
        for sx in range(0, tw, 4):
            w, h = min(4, tw - sx), min(4, th - sy)
            sprites.append((y0 + sy * 8 + 128, ((w - 1) << 10) | ((h - 1) << 8), len(tiles), x0 + sx * 8 + 128))
            for cx in range(sx, sx + w):
                for cy in range(sy, sy + h):
                    tiles.append(area[cy * 8:cy * 8 + 8, cx * 8:cx * 8 + 8])
    mask = lambda t, v: bytes(int(''.join('1' if p == v else '0' for p in row), 2) for row in t)
    data = struct.pack('>HH', len(tiles), len(sprites)) + b''.join(struct.pack('>4H', *s) for s in sprites)
    data += b''.join(mask(t, 1) for t in tiles) + b''.join(mask(t, 2) for t in tiles)
    open(out, 'wb').write(data)
    return len(tiles), len(sprites), (x0 - F.WX, y0 - F.WY, tw * 8, th * 8)


def sprite_block(idx, base=48):
    """An indexed screen image whose pixels are sprite colours (index - base, 1-15) -> (bytes, centre): u16 tiles,
    u16 sprites; per sprite u16 y + 128, size, first tile, x + 128; then the 4-bit tiles.  Sprites are at most
    4 x 4 tiles, tiles going down each sprite's columns."""
    import struct
    import encode_poc as E
    ys, xs = np.nonzero(idx)
    y0, x0, y1, x1 = int(ys.min()), int(xs.min()), int(ys.max()), int(xs.max())
    tw, th = (x1 - x0) // 8 + 1, (y1 - y0) // 8 + 1
    area = np.zeros((th * 8, tw * 8), np.uint8)
    area[:y1 - y0 + 1, :x1 - x0 + 1] = np.where(idx[y0:y1 + 1, x0:x1 + 1] > base, idx[y0:y1 + 1, x0:x1 + 1] - base, 0)
    sprites, tiles = [], []
    for sy in range(0, th, 4):
        for sx in range(0, tw, 4):
            w, h = min(4, tw - sx), min(4, th - sy)
            sprites.append((y0 + sy * 8 + 128, ((w - 1) << 10) | ((h - 1) << 8), len(tiles), x0 + sx * 8 + 128))
            for cx in range(sx, sx + w):
                for cy in range(sy, sy + h):
                    tiles.append(E.pack_tile(area[cy * 8:cy * 8 + 8, cx * 8:cx * 8 + 8].reshape(-1)))
    data = struct.pack('>HH', len(tiles), len(sprites)) + b''.join(struct.pack('>4H', *s) for s in sprites)
    return data + b''.join(tiles), ((x0 + x1) / 2, (y0 + y1) / 2)


def title(out):
    """The start screen: its stream (out/set/TITLE.bin) and the selection outlines as sprites (TITLE.SPR: u16
    buttons, u16 offset of each button's sprite block, the blocks; title.json: actions, clips, centres)."""
    import struct
    import scd_format as F
    name, size, msg = pack_unit(('MENUS', 'TITLE', True, True))
    print(f'  TITLE: {msg}')
    d = os.path.join(SCD, 'MENUS', 'TITLE')
    u = json.load(open(os.path.join(d, 'unit.json')))
    blocks, info = [], []
    for b in u['title']:
        data, centre = sprite_block(F.read_bmp(os.path.join(d, b['outline']))[0])
        blocks.append(data); info.append(dict(b, centre=centre))
    head = 2 + 2 * len(blocks)
    offs, pos = [], head
    for blk in blocks:
        offs.append(pos); pos += len(blk) + (len(blk) & 1)
    data = struct.pack('>H', len(blocks)) + b''.join(struct.pack('>H', o) for o in offs)
    data += b''.join(blk + bytes(len(blk) & 1) for blk in blocks)
    open(os.path.join(out, 'TITLE.SPR'), 'wb').write(data)
    json.dump(info, open(os.path.join(out, 'title.json'), 'w'), indent=1)
    print(f'  TITLE.SPR: {len(blocks)} outlines, {len(data)} bytes')


def cheat(out):
    """The cheat menu: its stream (out/set/CHEATMENU.bin) and the selection boxes as sprites (CHEAT.SPR: as TITLE.SPR,
    u16 entries, u16 offset of each entry's sprite block, the blocks; cheat.json: maps, centres)."""
    import struct
    import scd_format as F
    d = os.path.join(SCD, 'MENUS', 'CHEATMENU')
    if not os.path.exists(os.path.join(d, 'unit.json')):
        print('  no cheat menu (python scd_bitmaps.py CHEAT)')
        return
    name, size, msg = pack_unit(('MENUS', 'CHEATMENU', True, True))
    print(f'  CHEATMENU: {msg}')
    u = json.load(open(os.path.join(d, 'unit.json')))
    blocks, info = [], []
    for e in u['cheat']:
        data, centre = sprite_block(F.read_bmp(os.path.join(d, e['box']))[0])
        blocks.append(data); info.append(dict(e, centre=centre))
    head = 2 + 2 * len(blocks)
    offs, pos = [], head
    for blk in blocks:
        offs.append(pos); pos += len(blk) + (len(blk) & 1)
    data = struct.pack('>H', len(blocks)) + b''.join(struct.pack('>H', o) for o in offs)
    data += b''.join(blk + bytes(len(blk) & 1) for blk in blocks)
    open(os.path.join(out, 'CHEAT.SPR'), 'wb').write(data)
    json.dump(info, open(os.path.join(out, 'cheat.json'), 'w'), indent=1)
    print(f'  CHEAT.SPR: {len(blocks)} boxes, {len(data)} bytes')


def menus():
    """Step 3 for scd/MENUS: the save menu stream (out/set/SAVEMENU.bin) and the pause overlay (out/menus/
    PAUSE.SPR); build_disc.py puts both on the disc."""
    os.makedirs(os.path.join(HERE, 'out', 'menus'), exist_ok=True)
    name, size, msg = pack_unit(('MENUS', 'SAVEMENU', True, True))
    print(f'  SAVEMENU: {msg}')
    n, s, box = pause_sprite(os.path.join(SCD, 'MENUS', 'pause.bmp'), os.path.join(HERE, 'out', 'menus', 'PAUSE.SPR'))
    print(f'  PAUSE.SPR: {n} tiles x 2 colours, {s} sprites per colour, at window x {box[0]} y {box[1]} ({box[2]}x{box[3]})')
    title(os.path.join(HERE, 'out', 'menus'))
    cheat(os.path.join(HERE, 'out', 'menus'))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    workers = 4
    if '--workers' in sys.argv:
        workers = int(sys.argv[sys.argv.index('--workers') + 1]); args.remove(str(workers))
    stage, only = args[0].upper(), set(args[1:])
    os.makedirs(OUTDIR, exist_ok=True)
    if stage == 'MENUS':
        menus(); return
    if stage == 'CHEAT':                                # the cheat menu alone
        os.makedirs(os.path.join(HERE, 'out', 'menus'), exist_ok=True)
        cheat(os.path.join(HERE, 'out', 'menus')); return
    stages = sorted(d for d in os.listdir(SCD) if d.startswith('BD')) if stage == 'ALL' else [stage]
    jobs = [(st, n, '--force' in sys.argv, '--no-verify' not in sys.argv) for st in stages
            for n in sorted(os.listdir(os.path.join(SCD, st))) if not only or n in only]
    t0 = time.time()
    with Pool(workers) as p:
        for name, size, msg in p.imap_unordered(pack_unit, jobs):
            print(f'  {name}: {msg}', flush=True)
    # the unit table with the game logic, for build_disc.py
    import encode_set as ES
    logic, units = ES.units_of(stage)
    table = ES.resolve(logic, units)
    missing = []
    for u in table:
        f = os.path.join(OUTDIR, u['name'] + '.bin')
        uf = os.path.join(SCD, u['name'][:4], u['name'], 'unit.json')
        if not os.path.exists(uf) or not os.path.exists(f) or os.path.getmtime(f) < os.path.getmtime(uf):
            missing.append(u['name']); continue
        uj = json.load(open(uf))
        u['file'], u['bytes'] = f, os.path.getsize(f)
        u['scenes'] = [(s['first'], s['frames'], s['kind']) for s in uj['scenes']]
    if missing:
        print('not packed yet (no table written):', ', '.join(missing)); return
    out = os.path.join(HERE, 'out', f'set_{stage}.json')
    json.dump(table, open(out, 'w'), indent=1)
    tot = sum(u['bytes'] for u in table); fr = sum(u['frames'] for u in table)
    print(f'{out}: {len(table)} units, {fr} frames, {tot / 2 ** 20:.1f} MB, {tot / (fr / 12) / 1024:.0f} KB/s '
          f'average ({time.time() - t0:.0f} s)')


if __name__ == '__main__':
    main()

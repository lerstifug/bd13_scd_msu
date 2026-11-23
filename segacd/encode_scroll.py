"""Encoder for a scrolling scene (the camera moves vertically over a tall background), stream format version 5.

  python encode_scroll.py BD04_001 96 273   -> out/<clip>_<first>_scroll.bin (+ _preview.png)

The 3DO scene is a virtual background taller than the screen (BACK + the MVBG strips, see scroll_src.py) and a
camera track (MOVE).  On the Genesis both planes scroll vertically by the camera position; the 32-row plane is a
ring, and each frame brings in the background rows ("bands", 8 lines) that come into view, written into plane rows
that are off screen.  Each plane row owns ROWK scene tiles (28 for plane B plus up to 5 plane A extras for the
cells that gain most).  The cel stays in screen space: its cells are placed at the plane cells under it.
Hardware sprites in a black palette colour cover the 16-line bands above and below the window, which would
otherwise show the scrolled planes.
"""
import os, struct, sys
import numpy as np
from PIL import Image
import encode_poc as E
import encode_v3 as V
import colour as C
import scroll_src as S

TW = V.TW
GROW = 192 / 216                                       # 3DO -> Genesis vertical scale (window 192 lines)


def scale_virtual(img):
    """Tall 3DO background (H,288,3) -> (round(H*192/216), 224, 3), averaged in linear light like the frames."""
    h = int(round(img.shape[0] * GROW))
    a = E.srgb2lin(img.astype(float))
    ch = [np.array(Image.fromarray(a[:, :, c].astype(np.float32), 'F').resize((E.WIN_W, h), Image.BOX)) for c in range(3)]
    return np.clip(E.lin2srgb(np.dstack(ch)), 0, 255)


def band_rows(img, b, K):
    """The 8 lines of band b (virtual rows 8b-K .. 8b-K+7), edge rows repeated outside the image."""
    v = np.clip(np.arange(8 * b - K, 8 * b - K + 8), 0, len(img) - 1)
    return img[v]


def ensure_black(codes):
    """The border sprites need a black palette colour (not line 3 colours 14/15, which are sprite operators).
    Returns (codes, mask word = line << 8 | colour)."""
    for l in range(4):
        for j, c in enumerate(codes[l]):
            if c == 0 and not (l == 3 and j + 1 >= 14):
                return codes, (l << 8) | (j + 1)
    c0 = list(codes[0])
    if len(c0) < 15:
        c0.append(0)
    else:
        c0[int(np.argmin(C.LAB[c0][:, 0]))] = 0          # the darkest colour of line 0 becomes black
    codes = [np.array(c0)] + list(codes[1:])
    return codes, (0 << 8) | (list(c0).index(0) + 1)


def encode_band(T, codes, m):
    """One plane row: T (28,64,3) OKLab.  Plane B for every cell plus plane A for the cells that gain most,
    within ROWK tiles.  Returns tiles, map B, map A, the colours plane B shows (28,64,3), modes (28,)."""
    n = len(T)
    D2 = V.dists2(T, codes)
    best = []
    for md in (0, 1):
        e1m = D2[md].sum(1)
        best.append(np.minimum(e1m.min(1), V.best_pair(D2[md])[1]) + e1m.min(1))
    smode = (best[1] < best[0]).astype(int)
    D = V.pick(D2, smode)
    e1 = D.sum(1); l1 = e1.argmin(1)
    pr, e2 = V.best_pair(D)
    gain = e1[np.arange(n), l1] - e2
    pbl, pal_ = V.pair_lines(D, np.arange(n), pr, e1)
    cel, th = np.zeros((n, 64), bool), np.broadcast_to(C.THRESH[0], (n, 64))
    up = np.zeros(n, bool)
    order = [c for c in np.argsort(-gain) if gain[c] > 0]
    k = len(order)
    while True:                                         # most upgrades that still fit in ROWK tiles
        up[:] = False
        up[order[:k]] = True
        bline = np.where(up, pbl, l1)
        bpx, apx = V.dither_cells(T, cel, th, codes, bline, np.where(up, pal_, -1), smode)
        tiles, seen = [], {}

        def add(px):
            px = px.reshape(8, 8)
            for v, fl in ((px, 0), (px[:, ::-1], 0x800), (px[::-1], 0x1000), (px[::-1, ::-1], 0x1800)):
                if v.tobytes() in seen:
                    return fl | V.vram_tile(m * V.ROWK + 1 + seen[v.tobytes()])
            seen[px.tobytes()] = len(tiles)
            tiles.append(E.pack_tile(px.reshape(-1)))
            return V.vram_tile(m * V.ROWK + min(len(tiles), V.ROWK))
        prio = np.where(smode == 0, 0x8000, 0)
        mapB = [int(prio[c] | (int(bline[c]) << 13) | add(bpx[c])) for c in range(n)]
        mapA = [int(prio[c] | (int(pal_[c]) << 13) | add(apx[c])) if up[c] and apx[c].any() else 0 for c in range(n)]
        if len(tiles) <= V.ROWK:
            break
        k = max(0, k - (len(tiles) - V.ROWK))
    Bcol = V.colours(codes, bline, bpx, smode)
    return tiles, mapB, mapA, Bcol, smode, (bpx, apx, bline, np.where(up & apx.any(1), pal_, -1))


def scroll_scene(clip, first, last, frames=None):
    """Frames first..last of a clip as one scrolling scene: the scene record and the frame records (audio left
    as zeros for encode_v3.set_audio).  Returns recs, camera track, K, the virtual background, stats."""
    V.set_layout(410)                                   # the plane-row blocks need 32 x ROWK = 1056 scene tiles
    track = S.camera_track(clip, first, last)
    vimg, top = S.virtual_bg(clip, first, last)
    virt = scale_virtual(vimg)                          # (Hg, 224, 3)
    Hg = len(virt)
    frames = frames if frames is not None else V.load_frames(clip, first, last)
    n = len(frames)
    cam = [int(np.clip(round((track[first + i][1] - top) * GROW), 0, Hg - E.WIN_H)) for i in range(n)]
    K = (16 - cam[0]) % 256                             # plane line = virtual line + K (scroll 0 at the start)
    vlab = E.oklab(virt).astype(np.float32)
    # palette: the whole virtual background plus sampled cel frames
    bands = range((0 + K) // 8, (Hg - 1 + K) // 8 + 1)
    bandT = {b: E.tiles(band_rows(vlab, b, K)).astype(np.float32) for b in bands}
    comps = []
    for i, (f, new, bg, cel) in enumerate(frames):
        if cel is None:
            comps.append(None); continue
        rgb, op = cel
        comps.append((E.oklab(rgb).astype(np.float32), op))
    sample = [bandT[b] for b in bands]
    wts = [np.ones((TW, 64)) for b in bands]
    for i in range(0, n, 3):
        if comps[i] is None:
            continue
        lab, op = comps[i]
        Tc = E.tiles(lab); cp = E.tiles(op[:, :, None])[:, :, 0]
        m = cp.any(1)
        sample.append(Tc[m]); wts.append(np.where(cp[m], C.CEL_WEIGHT, 1.0))
    Sm, W = np.concatenate(sample), np.concatenate(wts)
    if len(Sm) > 4000:
        k = np.random.default_rng(0).choice(len(Sm), 4000, replace=False)
        Sm, W = Sm[k], W[k]
    codes, mask = ensure_black(V.fit_palette(Sm, W))
    enc = {b: encode_band(bandT[b], codes, b % 32) for b in bands}
    recs = []
    needed = lambda c: range((c + K) // 8, (c + E.WIN_H - 1 + K) // 8 + 1)
    ring = {}
    # scene record: palette, mask, scroll, the ring rows the first frame shows
    mapB, mapA = np.zeros((32, TW), int), np.zeros((32, TW), int)
    tiles = [bytes(32)] * (32 * V.ROWK)
    for b in needed(cam[0]):
        m = b % 32
        t, mb, ma, _, _, _ = enc[b]
        mapB[m], mapA[m] = mb, ma
        tiles[m * V.ROWK:m * V.ROWK + len(t)] = t
        ring[m] = b
    pal = [0] * 64
    for l in range(4):
        for j, c in enumerate(codes[l]):
            pal[l * 16 + 1 + j] = E.cram_word(int(c))
    rec = bytearray(b''.join(struct.pack('>H', w) for w in pal))
    rec += struct.pack('>HHHH', len(tiles), mask, (cam[0] + K - 16) % 256, V.POOLMAX)
    rec += b''.join(struct.pack('>H', int(w)) for w in mapB.reshape(-1)) + b''.join(struct.pack('>H', int(w)) for w in mapA.reshape(-1))
    rec += b''.join(tiles)
    recs.append(V.record(5, rec))
    stats = {'bytes': [], 'rows': [], 'tiles': []}
    prev = {}
    for i in range(n):
        c = cam[i]
        rows = []
        for b in needed(c):
            m = b % 32
            if ring.get(m) != b:
                t, mb, ma, _, _, _ = enc[b]
                rows.append((m, mb, ma, t)); ring[m] = b
        # changed cells: the plane cells under the cel, composited in virtual space
        T, cel, widx, keys, Bc, sm = [], [], [], [], [], []
        if comps[i] is not None:
            lab, op = comps[i]
            for b in needed(c):
                v = np.arange(8 * b - K, 8 * b - K + 8)
                r = v - c                                 # window row of each line (-: above, >=192: below)
                inwin = (r >= 0) & (r < E.WIN_H)
                if not inwin.any():
                    continue
                rr = np.clip(r, 0, E.WIN_H - 1)
                bl = band_rows(vlab, b, K).copy()
                o = op[rr] & inwin[:, None]
                bl[o] = lab[rr][o]
                Tb, Ob = E.tiles(bl), E.tiles(o[:, :, None])[:, :, 0]
                t, mb, ma, Bcol, smode, _ = enc[b]
                for col in np.where(Ob.any(1))[0]:
                    T.append(Tb[col]); cel.append(Ob[col]); widx.append((b % 32) * 32 + col + 2)
                    keys.append((b, int(col))); Bc.append(Bcol[col]); sm.append(smode[col])
        if T:
            entries, bits, tl = V.encode_cells(np.array(T, np.float32), np.array(cel), np.array(widx), keys, codes,
                                               np.array(Bc, np.float32), np.array(sm), prev)
        else:
            entries, bits, tl = [], [], []
            prev.clear()
        body, prev_pool = V.frame_body(bytes(E.AUD_PER_FRAME), entries, bits, tl, (c + K - 16) % 256, rows,
                                       prev_pool=None if i == 0 else prev_pool)
        recs.append(V.record(2, body))
        stats['bytes'].append(len(body) + 6); stats['rows'].append(len(rows)); stats['tiles'].append(len(tl))
        if i == 0:
            assert len(rec) + len(body) < 150000, 'scene + first frame must fit the 192 KB look-ahead'
    return recs, cam, K, virt, stats


def encode(clip, first, last, out_path):
    """One scrolling scene as a looping stream (for testing it on its own)."""
    frames = V.load_frames(clip, first, last)
    recs, cam, K, virt, stats = scroll_scene(clip, first, last, frames)
    audio = V.load_audio_3do(clip, len(frames), first)
    stream = V.write_stream(V.set_audio(recs, audio), len(frames), 1, out_path)
    return stream, frames, cam, K, virt, stats


if __name__ == '__main__':
    clip, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    out = os.path.join(E.HERE, 'out', f'{clip}_{a}_scroll.bin')
    stream, frames, cam, K, virt, st = encode(clip, a, b, out)
    fb = np.array(st['bytes'])
    dur = len(frames) / E.FPS
    print(f'{clip} {a}-{b}: {len(frames)} frames ({dur:.1f} s), stream {len(stream) / 1024:.0f} KB, '
          f'average {len(stream) / dur / 1024:.0f} KB/s; frame bytes mean {fb.mean():.0f} max {fb.max()}; '
          f'rows per frame max {max(st["rows"])}; frame tiles max {max(st["tiles"])}')
    imgs = V.simulate(stream)
    errs, shots = [], []
    for i in range(0, len(frames), 6):
        ref = virt[cam[i]:cam[i] + E.WIN_H].copy()
        cel = frames[i][3]
        if cel is not None:
            ref[cel[1]] = cel[0][cel[1]]
        errs.append(E.delta_e(ref, imgs[i]))
        if i in (0, 12, 18, 24, 30, 36, 48, 120):
            shots.append((ref.astype(np.uint8), imgs[i]))
    print(f'preview delta-E: mean {np.mean(errs):.2f} max {np.max(errs):.2f}; hidden plane A cells {V.HIDDEN[0]}')
    c = Image.new('RGB', (E.WIN_W * 2 + 8, E.WIN_H * len(shots)), (255, 255, 255))
    for r, (ref, img) in enumerate(shots):
        c.paste(Image.fromarray(ref), (0, r * E.WIN_H)); c.paste(Image.fromarray(img), (E.WIN_W + 8, r * E.WIN_H))
    c.save(out.replace('.bin', '_preview.png'))

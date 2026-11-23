"""Encoder v3 for the Sega CD player: two-layer tiles, full clip, looping.

  python encode_v3.py BD04_011 [frames] [tone]   -> out/<clip>_v3.bin + out/<clip>_v3_preview.png

Everything comes from the 3DO version: background and cel bitmaps, and the clip's own 3DO soundtrack.

Why two layers: a Genesis tile can only use one 15-colour palette line, so neighbouring 8x8 cells that picked
different lines show as visible blocks.  Here every cell may use plane B (bottom) AND plane A (on top, colour 0 =
see-through) with two different lines, so each pixel picks from up to 30 colours.  The background and the cel are
composited offline; all four palette lines serve the whole picture.

The sub CPU "renders" each frame: it keeps the scene's base maps, patches in the cells that changed, and leaves
complete plane maps + tile data in Word RAM, which the main CPU only DMAs to the video chip.

Shadow mode: the video chip runs with Shadow/Highlight on.  A cell whose plane A and plane B tiles both have
priority 0 is shown shadowed (every colour at about half brightness, with the DAC's finer dark steps 0, 29, 52,
70, 87, 101, 116, 130); priority 1 on either tile shows it normally.  The encoder picks normal or shadow per cell
and per frame, so every palette colour is also available at half brightness - most of this game is dark.  A cell
keeps its previous mode unless switching is clearly better (no flicker).  Sprites are not used, so all 60 colours
stay free (line 3 colours 14/15 are only special for sprites).

STREAM FORMAT (version 5, big-endian, word aligned)
  header  'BDSC', u8 version(5), u8 fps, u16 audio_rate, u16 frames, u8 tiles_w, u8 tiles_h, u16 scenes, u16 pad
  record  u16 type, u32 body length (even), body.   type 0 = end of stream (the player loops to the start)
  PREROLL (4): first record only: the audio of frames 0..AUD_LEAD-1 (played before frame 0's record arrives; the
             player uses it once and skips it when it loops)
  Audio travels AUD_LEAD frames early: frame f's record carries the audio of frame (f + AUD_LEAD) mod frames, so
  the sub CPU needs no look-ahead for sound and the loop is seamless.
  SCENE (1): 64 CRAM words | u16 ntiles, u16 0 | map B: 672 words | map A: 672 words | ntiles*32 tile bytes
             map words are final VRAM words (priority = normal/shadow, line bits 13-14, flips, tile number); scene
             tile k (1-based) lives at VRAM tile vram_tile(k).  A map word 0 = nothing on plane A.
             The window is plane rows 2-25, columns 2-29; nothing scrolls.
  SCROLL SCENE (5): a scene whose camera moves vertically (see encode_scroll.py): 64 CRAM words | u16 ntiles,
             u16 mask (line << 8 | colour of the black used by the border sprites), u16 vertical scroll, u16 0 |
             map B: 32 rows x 28 words | map A: 32 x 28 | ntiles*32 tile bytes (plane row r's tiles are scene
             tiles r*ROWK+1 ...).  Both planes scroll; 14 sprites cover the 16-line bands above and below the window.
  FRAME (2): 1200 audio bytes | u16 ncells, u16 ntiles, u16 vertical scroll, u16 nrows |
             nrows x (u16 plane row, u16 tiles, 28 B map words, 28 A map words) | ncells entry words | mode words |
             ntiles*32 frame tile bytes | the rows' tiles (row r: scene tiles r*ROWK+1 ...)
             rows replace plane rows of the base maps (scrolling scenes bring in the rows coming into view).
             entry: bits 0-9 word index into the 32x32 plane maps (row*32 + col), bit 15 new B tile,
             bits 13-14 B line, bit 12 new A tile, bits 10-11 A line.  Tiles follow in entry order, B before A.
             mode words: ceil(ncells/16) words, bit (i mod 16) of word i/16 = entry i is shown normally (sets the
             priority of its new B tile; entries without a new B tile keep the scene cell's mode).
             A cell listed without a new A tile gets an empty plane A; without a new B tile it keeps the scene's.
             Cells not listed show the scene's base maps.  Frame tiles go to the pool the sub CPU picks
             (alternately VRAM tile 716 and 1126, 410 tiles each).
"""
import os, sys, struct, wave
import numpy as np
import scroll_src
from PIL import Image
import encode_poc as E
import colour as C

TW, TH, NT = E.TW, E.TH, E.NT
def set_layout(P=410):
    """VRAM layout of a scene with frame pools of P tiles: the two pools sit at the top of tiles 1-1535, scene
    tiles fill the rest of 1-1535 plus 1600-1791 and 1856-2015 (see payload.s).  P = 410 gives 1067 scene tiles;
    scenes whose cels cover most of the screen use bigger pools and fewer scene tiles."""
    global REGIONS, SCENE_MAX, POOLS, POOLMAX
    r1 = 1535 - 2 * P
    REGIONS = ((1, r1), (1600, 192), (1856, 160))  # VRAM tile ranges for scene tiles
    SCENE_MAX = r1 + 192 + 160
    POOLS = (r1 + 1, r1 + 1 + P)
    POOLMAX = P


set_layout(410)


def set_layout_wide():
    """H40 menu screens (scene record type 6): scene tiles are VRAM tiles 1-1399 in order, 1400-1535 hold sprite
    tiles (selection outlines); no frame pools."""
    global REGIONS, SCENE_MAX, POOLS, POOLMAX
    REGIONS = ((1, 1399),)
    SCENE_MAX = 1399
    POOLS = (1400, 1400)
    POOLMAX = 136


PBIG = 560      # pools of scenes with cels over 410 cells: leaves 767 scene tiles, more than one per window cell;
                # frames whose distinct tiles still exceed it merge their most similar tiles (encode_cells)
FRAME_CAP = 300                                     # two-layer upgrades stop here (CD bandwidth)
ROWK = 33                                           # scrolling scenes: scene tiles per plane row (32 x 33 = 1056)
AUD_LEAD = 6                                       # frames of audio queued ahead in the PCM ring (12 chunks)
TONE = 'tone' in sys.argv[1:]                       # replace the soundtrack with a 1 kHz calibration tone
DITHER_AMP = 0.045
PAIRS = [(a, b) for a in range(4) for b in range(a + 1, 4)]
# word index of each window cell in the 32x32 plane maps: the window is plane rows 2-25, columns 2-29
CELL_IDX = np.array([(c // TW + 2) * 32 + c % TW + 2 for c in range(NT)])


def vram_tile(k):
    for base, n in REGIONS:
        if k <= n:
            return base + k - 1
        k -= n
    raise ValueError('too many scene tiles')


def line_dists(T, lines):
    """T (n,64,3) -> D (n,64,4) squared distance to the nearest colour of each line, J (n,64,4) its index."""
    D = np.empty(T.shape[:2] + (len(lines),), np.float32)
    J = np.empty(T.shape[:2] + (len(lines),), np.int16)
    for l, P in enumerate(lines):
        d = ((T[:, :, None, :] - P[None, None]) ** 2).sum(3)
        J[:, :, l] = d.argmin(2)
        D[:, :, l] = np.take_along_axis(d, J[:, :, l:l + 1].astype(np.int64), 2)[:, :, 0]
    return D, J


def best_pair(D):
    """Per tile: best (a, b) line pair and its error, pixels taking the nearer of the two lines."""
    pairs = PAIRS if D.shape[2] == 4 else [(a, b) for a in range(D.shape[2]) for b in range(a + 1, D.shape[2])]
    E2 = np.stack([np.minimum(D[:, :, a], D[:, :, b]).sum(1) for a, b in pairs], 1)
    k = E2.argmin(1)
    return np.array(pairs)[k], E2[np.arange(len(D)), k]


def dists2(T, codes):
    """(2,n,64,4): squared distance to the nearest colour of each line, shown normally [0] and shadowed [1]."""
    return np.stack([line_dists(T, [tab[c] for c in codes])[0] for tab in (C.LAB, C.LABS)])


def pick(D2, m):
    """Per-cell mode m (n,) -> that mode's distances (n,64,4)."""
    return np.where(m[:, None, None] == 1, D2[1], D2[0])


def fit_palette(T, W, nl=4, shadow=True):
    """nl (4) 15-colour lines (grid codes) for tiles T (n,64,3 OKLab) with pixel weights W (n,64).  A pixel in a
    shadowed cell is served by the code whose *shadowed* colour is nearest, so it votes for that code
    (shadow=False: every cell shown normally)."""
    GN, GS = C.snap(T), C.snap(T, C.LABS)                             # nearest code per pixel, normal / shadowed
    eN = ((T - C.LAB[GN]) ** 2).sum((1, 2)); eS = ((T - C.LABS[GS]) ** 2).sum((1, 2))
    mode = (eS < eN).astype(int) if shadow else np.zeros(len(T), int)  # first guess per tile
    hist = lambda m, md: np.bincount(np.where(md, GS, GN)[m], W[m], minlength=512)
    feat = np.concatenate([T.mean(1), T.std(1)], 1)
    cent = E.kmeans(feat, nl)
    grp = ((feat[:, None] - cent[None]) ** 2).sum(2).argmin(1)
    mdp = np.repeat(mode[:, None], 64, 1).astype(bool)
    codes = [C.fit_line(hist(np.repeat((grp == l)[:, None], 64, 1), mdp)) if (grp == l).any()
             else C.fit_line(hist(np.ones_like(GN, bool), mdp)) for l in range(nl)]
    for _ in range(4):
        D2 = dists2(T, codes)
        res = [best_pair(D2[m]) for m in (0, 1)]
        mode = (res[1][1] < res[0][1]).astype(int) if shadow else np.zeros(len(T), int)
        pr = np.where(mode[:, None] == 1, res[1][0], res[0][0])
        D = pick(D2, mode)
        a, b = pr[:, 0], pr[:, 1]
        da = np.take_along_axis(D, a[:, None, None].repeat(64, 1), 2)[:, :, 0]
        db = np.take_along_axis(D, b[:, None, None].repeat(64, 1), 2)[:, :, 0]
        owner = np.where(da <= db, a[:, None], b[:, None])            # (n,64) line that serves each pixel
        mdp = np.repeat(mode[:, None], 64, 1).astype(bool)
        for l in range(nl):
            if (owner == l).sum() >= 15:
                codes[l] = C.fit_line(hist(owner == l, mdp))
    return codes


def dither_cells(T, cel, th, codes, bl, al, mode):
    """Pattern-dither cells T (n,64,3) for B lines bl (n,), A lines al (n,; -1 = no A tile) and modes (n,).
    Returns B pixels (n,64), A pixels (n,64; 0 = see-through) as palette indices 1..15."""
    n = len(T)
    bpx = np.zeros((n, 64), np.uint8); apx = np.zeros((n, 64), np.uint8)
    for md in (0, 1):
        for b in range(4):                                            # plane B alone, line b
            m = (bl == b) & (mode == md)
            if m.any():
                bpx[m] = C.pattern(T[m], cel[m], th[m], codes[b], md)[0] + 1
        for b in range(4):                                            # cells with both: pick from b + a together
            for a in range(4):
                m = (bl == b) & (al == a) & (mode == md)
                if not m.any():
                    continue
                nb = len(codes[b])
                ix = C.pattern(T[m], cel[m], th[m], np.concatenate([codes[b], codes[a]]), md)[0]
                onb = ix < nb
                bpx[m] = np.where(onb, ix + 1, bpx[m])               # B shows its own pick where A is see-through
                apx[m] = np.where(onb, 0, ix - nb + 1)
    return bpx, apx


class TilePool:
    """Scene tile store with flip-aware de-duplication.  Returns map-word bits (flips | VRAM tile)."""
    def __init__(self):
        self.tiles, self.seen = [], {}

    def add(self, px):
        px = px.reshape(8, 8)
        for v, fl in ((px, 0), (px[:, ::-1], 0x800), (px[::-1], 0x1000), (px[::-1, ::-1], 0x1800)):
            k = v.tobytes()
            if k in self.seen:
                return fl | vram_tile(self.seen[k])
        self.tiles.append(E.pack_tile(px.reshape(-1)))
        self.seen[px.tobytes()] = len(self.tiles)
        return vram_tile(min(len(self.tiles), SCENE_MAX))      # over budget: the caller checks and retries


def pair_lines(D, i, pr, e1):
    """B line of an upgraded cell = the better single line of its pair (plane B alone stays a good picture of
    the cell, since frames may later replace plane A there); A line = the other one."""
    a0, b0 = pr[:, 0], pr[:, 1]
    bl = np.where(e1[i, a0] <= e1[i, b0], a0, b0)
    return bl, np.where(bl == a0, b0, a0)


def colours(codes, line, px, mode):
    """OKLab colour of palette pixels px (n,64; 1..15) of lines line (n,) in modes mode (n,)."""
    return np.stack([C.TABLES[mode[c]][0][codes[line[c]]][px[c].astype(int) - 1] for c in range(len(px))])


def shown(bpx, apx, bline, aline, mode):
    """What cells show, as Sega CD bitmap indices (see scd_format.py: shadow * 64 + line * 16 + colour): plane A's
    pixel where it has one (aline >= 0, apx > 0), else plane B's.  All (n,64) / (n,)."""
    a = (aline[:, None] >= 0) & (apx > 0)
    return (mode[:, None] * 64 + np.where(a, aline[:, None] * 16 + apx, bline[:, None] * 16 + bpx)).astype(np.uint8)


def encode_scene(T, codes, shadow=True):
    """T (NT,64,3) OKLab background tiles.  Returns the tile pool, both base maps, the colours plane B shows,
    each cell's mode, the number of two-layer cells and the layers (bpx, apx, bline, aline; aline -1 = no A)."""
    D2 = dists2(T, codes)
    best = []
    for m in (0, 1):
        e1m = D2[m].sum(1)
        best.append(np.minimum(e1m.min(1), best_pair(D2[m])[1]) + e1m.min(1))
    smode = (best[1] < best[0]).astype(int) if shadow else np.zeros(NT, int)   # normal or shadowed, per cell
    D = pick(D2, smode)
    e1 = D.sum(1)
    l1 = e1.argmin(1)
    pr, e2 = best_pair(D)
    gain = e1[np.arange(NT), l1] - e2
    pbl, pal_ = pair_lines(D, np.arange(NT), pr, e1)
    cel, th = np.zeros((NT, 64), bool), C.THRESH
    none = np.full(NT, -1)
    # upgrade the cells that gain most, as far as the tile budget left after the de-duplicated B tiles allows
    up = gain > 0
    bpx, _ = dither_cells(T, cel, th, codes, np.where(up, pbl, l1), none, smode)
    room = SCENE_MAX - len({t.tobytes() for t in bpx})
    for k, c in enumerate([c for c in np.argsort(-gain) if up[c]]):
        if k >= room:
            up[c] = False
    prio = np.where(smode == 0, 0x8000, 0)
    while True:                       # upgraded cells' B tiles change too, so re-check the budget until it fits
        bline = np.where(up, pbl, l1)
        bpx, apx = dither_cells(T, cel, th, codes, bline, np.where(up, pal_, -1), smode)
        pool = TilePool()
        mapB = np.array([prio[c] | (int(bline[c]) << 13) | pool.add(bpx[c]) for c in range(NT)])
        mapA = np.array([prio[c] | (int(pal_[c]) << 13) | pool.add(apx[c]) if up[c] and apx[c].any() else 0
                         for c in range(NT)])
        over = len(pool.tiles) - SCENE_MAX
        if over <= 0:
            break
        if not up.any():
            # even one plane B tile per cell is too many (small scene budget next to big frame pools): merge the
            # most similar background tiles; the cells adopt the kept tile's line and mode
            rep = merge_to_budget(colours(codes, bline, bpx, smode), SCENE_MAX - 8)
            bpx, bline, smode = bpx[rep], bline[rep], smode[rep]
            prio = np.where(smode == 0, 0x8000, 0)
            pool = TilePool()
            mapB = np.array([prio[c] | (int(bline[c]) << 13) | pool.add(bpx[c]) for c in range(NT)])
            mapA = np.zeros(NT, int)
            up[:] = False
            break
        for c in [c for c in np.argsort(gain) if up[c]][:over]:
            up[c] = False
    Bcol = colours(codes, bline, bpx, smode)          # what plane B shows (frames may replace plane A there)
    aline = np.where(up & apx.any(1), pal_, -1)
    return pool, mapB, mapA, Bcol, smode, int(up.sum()), (bpx, apx, bline, aline)


HYST = 1.10                                           # switching a cell's mode must win by 10% (no flicker)


def encode_frame(comp, celpx, cells, codes, Bcol, smode, prev):
    """Static scene: comp (NT,64,3) composited OKLab tiles; celpx (NT,64) cel pixels; cells: cells with cel
    pixels; Bcol/smode: what the scene's plane B shows and its mode, per cell."""
    return encode_cells(comp[cells], celpx[cells], CELL_IDX[cells], [int(c) for c in cells], codes,
                        Bcol[cells], smode[cells], prev)


def encode_cells(T, cel, widx, keys, codes, Bcol, sm, prev, Bidx=None):
    """Encode the changed cells of one frame.  T (n,64,3) composited OKLab; cel (n,64) cel pixels; widx (n,)
    word index of each cell in the 32x32 plane map; keys: cell identities for the flicker guard; Bcol (n,64,3) /
    sm (n,): what the base plane B shows in each cell and its mode; prev: {key: mode} of the previous frame's
    cells (updated here).  Returns entries, mode words, tiles; with Bidx (n,64: the base plane B as bitmap
    indices) also what each cell shows, as bitmap indices (n,64)."""
    th = np.broadcast_to(C.THRESH[0], (len(T), 64))      # every cell has the same Bayer pattern (8 = 2 x 4)
    D2 = dists2(T, codes)
    n = len(T)
    pref = np.array([prev.get(k, m) for k, m in zip(keys, sm)], int)
    bias = lambda m: np.where(pref == m, 1.0, HYST)
    e1s = [D2[m].sum(1) for m in (0, 1)]
    ea_m = np.stack([e1s[m].min(1) * bias(m) for m in (0, 1)], 1)
    ma = ea_m.argmin(1)                                                                # (a) new B only
    la = np.where(ma == 1, e1s[1].argmin(1), e1s[0].argmin(1))
    ea = ea_m[np.arange(n), ma]
    prs = [best_pair(D2[m]) for m in (0, 1)]                                           # (c) new B + new A
    ec_m = np.stack([prs[m][1] * bias(m) for m in (0, 1)], 1)
    mc = ec_m.argmin(1)
    ec = ec_m[np.arange(n), mc]
    pr = np.where(mc[:, None] == 1, prs[1][0], prs[0][0])
    Dsc = pick(D2, sm)                                                                 # (b) cel on A over base B
    dB = ((T - Bcol) ** 2).sum(2)
    eb_all = np.stack([np.where(cel, Dsc[:, :, l], dB).sum(1) for l in range(4)], 1) * bias(sm)[:, None]
    lb = eb_all.argmin(1); eb = eb_all[np.arange(n), lb]
    one = np.where(ea <= eb, 0, 1)
    e_one = np.minimum(ea, eb)
    room = FRAME_CAP - n
    up = np.zeros(n, bool)
    if room > 0:
        for i in np.argsort(-(e_one - ec))[:room]:
            if e_one[i] - ec[i] > 0:
                up[i] = True
    pbl, pal_ = pair_lines(pick(D2, mc), np.arange(n), pr, np.where(mc[:, None] == 1, e1s[1], e1s[0]))
    modeb = ~up & (one == 1)
    cmode = np.where(up, mc, np.where(modeb, sm, ma))
    bline = np.where(up, pbl, la)
    bpx, apx = dither_cells(T, cel, th, codes, bline, np.where(up, pal_, -1), cmode)
    for md in (0, 1):
        for l in range(4):                                                             # (b): cel pixels only
            m = modeb & (lb == l) & (cmode == md)
            if m.any():
                ix = C.pattern(T[m], cel[m], th[m], codes[l], md)[0] + 1
                apx[m] = np.where(cel[m], ix, 0)
    entries, tl = [], []
    bits = np.zeros((n + 15) // 16, int)
    for i in range(n):
        e = int(widx[i])
        if not modeb[i]:
            tl.append(E.pack_tile(bpx[i])); e |= 0x8000 | (int(bline[i]) << 13)
            if cmode[i] == 0:
                bits[i // 16] |= 1 << (i % 16)
        if (up[i] or modeb[i]) and apx[i].any():
            tl.append(E.pack_tile(apx[i])); e |= 0x1000 | (int(pal_[i] if up[i] else lb[i]) << 10)
        entries.append(e)
    prev.clear()
    if len(set(tl)) > POOLMAX:
        # too many different tiles for the pool (a cel covering most of the screen): every cell gets one plane B
        # tile, and the most similar tiles are merged until they fit (identical tiles share a VRAM tile)
        bpx1, _ = dither_cells(T, cel, th, codes, la, np.full(n, -1), ma)
        rep = merge_to_budget(colours(codes, la, bpx1, ma), POOLMAX)
        entries, tl = [], []
        bits = np.zeros((n + 15) // 16, int)
        for i in range(n):
            r = rep[i]
            entries.append(int(widx[i]) | 0x8000 | (int(la[r]) << 13))
            tl.append(E.pack_tile(bpx1[r]))
            if ma[r] == 0:
                bits[i // 16] |= 1 << (i % 16)
        cmode = ma[rep]
        disp = shown(bpx1[rep], np.zeros((n, 64), int), la[rep], np.full(n, -1), cmode)
    elif Bidx is not None:
        disp = shown(bpx, apx, bline, np.where(up, pal_, np.where(modeb, lb, -1)), cmode)
        disp[modeb] = np.where(apx[modeb] > 0, disp[modeb], Bidx[modeb])      # cel on A over the base plane B
    prev.update({k: int(cmode[i]) for i, k in enumerate(keys)})
    if Bidx is not None:
        return entries, [int(b) for b in bits], tl, disp
    return entries, [int(b) for b in bits], tl


def pack_tiles(tl, prev_pool):
    """Tile slots -> (flag words, reference words, new tile data, this frame's pool).  A slot whose tile already
    appeared in this frame refers to that slot (bits 0-9) and shares its VRAM tile; a tile found in the previous
    frame's pool refers to it (bit 15 + pool index) and is copied by the sub CPU into this frame's pool; anything
    else is new data.  pool: {tile bytes: pool index}."""
    flags = [0] * ((len(tl) + 15) // 16)
    refs, new, first, pool = [], [], {}, {}
    for j, t in enumerate(tl):
        if t in first:
            refs.append(first[t])
        elif prev_pool and t in prev_pool:
            refs.append(0x8000 | prev_pool[t])
            first[t], pool[t] = j, len(pool)
        else:
            new.append(t)
            first[t], pool[t] = j, len(pool)
            continue
        flags[j // 16] |= 1 << (j % 16)
    return flags, refs, new, pool


def merge_to_budget(R, budget, w=None):
    """Greedy merge of similar tiles: R (n,64,3) rendered OKLab.  Returns rep (n,) with at most budget distinct
    values; each merged tile is replaced by the closest kept one (the one used by more cells survives)."""
    n = len(R)
    rep = np.arange(n)
    if n <= budget:
        return rep
    w = np.ones(n) if w is None else np.asarray(w, float).copy()
    X = R.reshape(n, -1).astype(np.float32)
    D = (X * X).sum(1)[:, None] + (X * X).sum(1)[None] - 2 * X @ X.T
    np.fill_diagonal(D, np.inf)
    for _ in range(n - budget):
        i, j = np.unravel_index(np.argmin(D), D.shape)
        if w[j] > w[i]:
            i, j = j, i
        rep[rep == j] = i; w[i] += w[j]
        D[j, :] = np.inf; D[:, j] = np.inf
    return rep


def frame_body(audio, entries, bits, tl, vs=0, rows=(), prev_pool=None):
    """FRAME record body and this frame's pool; rows: (plane row, 28 B map words, 28 A map words, tiles) for
    scrolling scenes; prev_pool: the previous frame's pool (None after a scene change or at the start)."""
    w = lambda ws: b''.join(struct.pack('>H', int(x) & 0xFFFF) for x in ws)
    flags, refs, new, pool = pack_tiles(tl, prev_pool)
    assert len(pool) <= POOLMAX, (len(pool), POOLMAX)
    body = bytearray(audio) + struct.pack('>HHHH', len(entries), len(tl), vs & 0xFF, len(rows))
    for m, mb, ma, rt in rows:
        body += struct.pack('>HH', m, len(rt)) + w(mb) + w(ma)
    body += w(entries) + w(bits)
    body += struct.pack('>H', len(refs)) + w(flags) + w(refs) + b''.join(new)
    for r in rows:
        body += b''.join(r[3])
    return body, pool


def load_audio_3do(clip, nframes, first=0):
    """The clip's own 3DO soundtrack (frame-locked to the 3DO video, see snd3do.py) -> mono 14.4 kHz in the
    RF5C164's sign-magnitude bytes, 1200 per frame.  Resampled in the frequency domain, so everything above the
    new 7.2 kHz limit is removed instead of folding back as aliasing."""
    if TONE:
        t = np.arange(nframes * E.AUD_PER_FRAME)
        v = np.rint(100 * np.sin(2 * np.pi * 1000 * t / E.AUD_RATE)).astype(int)
    else:
        path = os.path.join(E.clip_dir(clip), 'audio.wav')
        if not os.path.exists(path):
            sys.path.insert(0, os.path.join(E.HERE, '..', 'bd13', '3do'))
            import snd3do
            snd3do.extract(clip)
        w = wave.open(path)
        x = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float64)
        x = x.reshape(-1, w.getnchannels()).mean(1) / 32768
        n_src = int(round(nframes * w.getframerate() / E.FPS))
        x = x[int(round(first * w.getframerate() / E.FPS)):]          # the segment starting at video frame first
        x = np.pad(x[:n_src], (0, max(0, n_src - len(x))))
        n_out = nframes * E.AUD_PER_FRAME
        X = np.fft.rfft(x)[:n_out // 2 + 1]
        X[int(len(X) * 0.95):] = 0                                      # stop just below 7.2 kHz
        y = np.fft.irfft(X, n_out) * n_out / n_src
        v = np.clip(np.rint(y * 127), -126, 126).astype(int)
    # RF5C164 sign-magnitude (bit7 = 1 positive); 0xFF is the loop marker, so magnitude <= 0x7E
    return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)


def load_frames(clip, first=0, last=None, idx=None):
    """3DO frames first..last of a clip (or the frames idx, e.g. one path, in that order) as (frame, new
    background?, background, cel) like encode_poc.load_clip, but with 8-bit backgrounds (BAC8) and cels (CELL)
    too.  A new background clears the cel unless the same frame draws one; frames without a cel chunk keep the
    previous one (COPY)."""
    import csv
    d, dn = E.clip_dir(clip), E.clip_disc(clip)
    rows = [r for r in csv.DictReader(open(os.path.join(E.SRC3, 'manifest.csv')))
            if r['disc'] == dn and r['file'] == clip + '.DAT']
    last = len(rows) - 1 if last is None else last
    sel = [rows[i] for i in idx] if idx is not None else rows[first:last + 1]
    bg = cel = None
    out = []
    for r in sel:
        f, ch = int(r['frame']), r['chunks'].split()
        new = False
        for tag in ('BACK', 'BAC8'):
            p = os.path.join(d, f'{tag}_{f:05d}.png')
            if tag in ch and os.path.exists(p):
                im = Image.open(p).convert('RGB')
                g = scroll_src.gradient_lines(np.asarray(im))       # a plain vertical gradient: each line one
                if g is not None:                                   # colour, so a row of cells is one cell repeated
                    im = Image.fromarray(np.repeat(np.round(g).astype(np.uint8)[:, None], im.width, 1))
                bg, cel, new = E.scale_rgb(im), None, True
        for tag in ('CL16', 'CELL'):
            p = os.path.join(d, f'{tag}_{f:05d}.png')
            if tag in ch and os.path.exists(p):
                cel = E.scale_cel(Image.open(p))
        out.append((f, new, bg, cel))
    return out


def record(t, body):
    return struct.pack('>HI', t, len(body)) + bytes(body)


def static_scene(bg, cels, seed=0, st=None):
    """One static scene: bg (192,224,3) and its frames' cels [(index, cel)].  Returns the scene record and the
    frame records, whose audio is left as zeros (the assembler fills it in, see set_audio)."""
    bgT = E.tiles(E.oklab(bg)).astype(np.float32)
    ncel = max([int(E.tiles(c[1][:, :, None])[:, :, 0].any(1).sum()) for _, c in cels if c is not None] + [0])
    P = 410 if ncel <= 410 else PBIG
    set_layout(P)
    comps = []
    for i, cel in cels:
        if cel is None:
            comps.append((i, bgT, np.zeros((NT, 64), bool), np.zeros(0, int)))
            continue
        rgb, op = cel
        img = bg.copy(); img[op] = rgb[op]
        T = E.tiles(E.oklab(img)).astype(np.float32)
        cp = E.tiles(op[:, :, None])[:, :, 0]
        comps.append((i, T, cp, np.where(cp.any(1))[0]))
    sample = [bgT] + [T[c] for _, T, _, c in comps[::3] if len(c)]
    wts = [np.ones((NT, 64))] + [np.where(cp[c], C.CEL_WEIGHT, 1.0) for _, _, cp, c in comps[::3] if len(c)]
    S, W = np.concatenate(sample), np.concatenate(wts)
    if len(S) > 4000:
        k = np.random.default_rng(seed).choice(len(S), 4000, replace=False)
        S, W = S[k], W[k]
    codes = fit_palette(S, W)
    pool, mapB, mapA, Bcol, smode, nup, _ = encode_scene(bgT, codes)
    pal = [0] * 64
    for l in range(4):
        for j, c in enumerate(codes[l]):
            pal[l * 16 + 1 + j] = E.cram_word(int(c))
    rec = bytearray(b''.join(struct.pack('>H', w) for w in pal))
    rec += struct.pack('>HH', len(pool.tiles), P)
    rec += b''.join(struct.pack('>H', int(w)) for w in mapB) + b''.join(struct.pack('>H', int(w)) for w in mapA)
    rec += b''.join(pool.tiles)
    recs = [record(1, rec)]
    if st is not None:
        st.setdefault('scene', []).append((cels[0][0], len(pool.tiles), nup, len(rec), f'{smode.mean():.0%} shadowed'))
    prev, prev_pool = {}, None
    for n, (i, T, cp, cells) in enumerate(comps):
        entries, bits, tl = encode_frame(T, cp, cells, codes, Bcol, smode, prev)
        body, prev_pool = frame_body(bytes(E.AUD_PER_FRAME), entries, bits, tl, prev_pool=prev_pool if n else None)
        recs.append(record(2, body))
        if n == 0:
            assert len(rec) + len(body) < 150000, 'scene + first frame must fit the 192 KB look-ahead'
    return recs


SILENCE = bytes([0x80]) * E.AUD_PER_FRAME            # sign-magnitude zero


def audio_bytes(x, rate, nframes):
    """Mono float audio (-1..1) at rate, nframes video frames long -> 1200 RF5C164 bytes per frame at 14.4 kHz,
    resampled in the frequency domain (nothing above 7.2 kHz folds back)."""
    n_src = int(round(nframes * rate / E.FPS))
    x = np.pad(np.asarray(x, float)[:n_src], (0, max(0, n_src - len(x))))
    n_out = nframes * E.AUD_PER_FRAME
    X = np.fft.rfft(x)[:n_out // 2 + 1]
    X[int(len(X) * 0.95):] = 0                                          # stop just below 7.2 kHz
    y = np.fft.irfft(X, n_out) * n_out / n_src
    v = np.clip(np.rint(y * 127), -126, 126).astype(int)
    return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)


def set_audio(recs, audio, loop=False):
    """Fill in the audio: frame record f carries the audio of frame f + AUD_LEAD (silence past the end, or the
    stream's start again if loop), and every scene record gets a PREROLL appended to its body: the audio of its
    first AUD_LEAD frames, used when playback starts at that scene (a file start, a jump, a restart after pause).
    audio: 1200 bytes per frame, all frames."""
    A = E.AUD_PER_FRAME
    n = len(audio) // A
    aud = lambda f: audio[(f % n) * A:(f % n + 1) * A] if loop or f < n else SILENCE
    out, f = [], 0
    for r in recs:
        t = struct.unpack('>H', r[:2])[0]
        if t == 2:
            r = r[:6] + aud(f + AUD_LEAD) + r[6 + A:]
            f += 1
        elif t in (1, 5, 6):
            r = record(t, r[6:] + b''.join(aud(f + k) for k in range(AUD_LEAD)))
        out.append(r)
    assert f == n, (f, n)
    return out


def write_stream(recs, nframes, nscenes, path):
    stream = b'BDSC' + struct.pack('>BBHHBBHH', 5, E.FPS, E.AUD_RATE, nframes % 65536, TW, TH, nscenes, 0)
    stream += b''.join(recs) + struct.pack('>HI', 0, 0)
    open(path, 'wb').write(stream)
    return stream


def encode(clip, nframes, out_path):
    """A clip of static scenes (frames 0..nframes-1) as a looping stream."""
    frames = load_frames(clip, 0, nframes - 1)
    audio = load_audio_3do(clip, len(frames))
    scenes, cur = [], None
    for i, (f, new, bg, cel) in enumerate(frames):
        if new or cur is None:
            cur = {'bg': bg, 'cels': []}
            scenes.append(cur)
        cur['cels'].append((i, cel))
    st = {}
    recs = []
    for si, s in enumerate(scenes):
        recs += static_scene(s['bg'], s['cels'], si, st)
    sizes = [len(r) for r in recs if struct.unpack('>H', r[:2])[0] == 2]
    st['frame_bytes'] = sizes
    st['frame_tiles'] = [0]
    stream = write_stream(set_audio(recs, audio), len(frames), len(scenes), out_path)
    return stream, frames, st


# ---- preview: decode the stream exactly as the sub CPU + video chip would ------------------------------------
def simulate(stream):
    """Decode a version 5 stream into the 224x192 window of every frame."""
    assert stream[:4] == b'BDSC' and stream[4] == 5
    p, vram, pal = 16, {}, None
    baseB = baseA = None
    parity, vs, prev_data = 0, 0, []
    out = []
    tile = lambda b, o: (lambda a: np.stack([a >> 4, a & 15], 1).reshape(8, 8))(np.frombuffer(b[o:o + 32], np.uint8))
    while True:
        t, ln = struct.unpack('>HI', stream[p:p + 6]); b = stream[p + 6:p + 6 + ln]; p += 6 + ln
        if t == 0:
            return out
        if t == 4:
            continue
        if t == 1:                                        # static scene: window at plane rows 2-25
            pal = struct.unpack('>64H', b[:128]); nt, P = struct.unpack('>HH', b[128:132])
            set_layout(P)
            baseB, baseA = np.zeros(1024, int), np.zeros(1024, int)
            baseB[CELL_IDX] = struct.unpack('>672H', b[132:132 + 1344])
            baseA[CELL_IDX] = struct.unpack('>672H', b[1476:1476 + 1344])
            for k in range(nt):
                vram[vram_tile(k + 1)] = tile(b, 2820 + 32 * k)
            vs = 0
            continue
        if t == 5:                                        # scrolling scene: 32 ring rows of 28 cells
            pal = struct.unpack('>64H', b[:128])
            nt, mask, vs, P = struct.unpack('>HHHH', b[128:136])
            set_layout(P)
            cols = np.array([r * 32 + c + 2 for r in range(32) for c in range(TW)])
            baseB, baseA = np.zeros(1024, int), np.zeros(1024, int)
            baseB[cols] = struct.unpack('>896H', b[136:136 + 1792])
            baseA[cols] = struct.unpack('>896H', b[1928:1928 + 1792])
            for k in range(nt):
                vram[vram_tile(k + 1)] = tile(b, 3720 + 32 * k)
            continue
        nc, ntl, vs, nrows = struct.unpack('>HHHH', b[1200:1208]); o = 1208
        rows = []
        for _ in range(nrows):
            m, nt = struct.unpack('>HH', b[o:o + 4])
            mb = struct.unpack('>28H', b[o + 4:o + 60]); ma = struct.unpack('>28H', b[o + 60:o + 116]); o += 116
            baseB[m * 32 + 2:m * 32 + 30] = mb; baseA[m * 32 + 2:m * 32 + 30] = ma
            rows.append((m, nt))
        ent = struct.unpack('>%dH' % nc, b[o:o + 2 * nc]); o += 2 * nc
        nw = (nc + 15) // 16
        bits = struct.unpack('>%dH' % nw, b[o:o + 2 * nw]); o += 2 * nw
        nref = struct.unpack('>H', b[o:o + 2])[0]; o += 2
        fw = (ntl + 15) // 16
        flags = struct.unpack('>%dH' % fw, b[o:o + 2 * fw]); o += 2 * fw
        refs = struct.unpack('>%dH' % nref, b[o:o + 2 * nref]); o += 2 * nref
        slotp, data, r = [], [], 0                         # slot -> pool index; pool tile data
        for j in range(ntl):
            if flags[j // 16] >> (j % 16) & 1:
                ref = refs[r]; r += 1
                if ref & 0x8000:                           # copy of a tile of the previous frame's pool
                    data.append(prev_data[ref & 0x3FF]); slotp.append(len(data) - 1)
                else:                                      # same tile as an earlier slot: shares its VRAM tile
                    slotp.append(slotp[ref & 0x3FF])
            else:
                data.append(b[o:o + 32]); o += 32; slotp.append(len(data) - 1)
        prev_data = data
        pool = POOLS[parity]; parity ^= 1
        mB, mA = baseB.copy(), baseA.copy()
        k = 0
        for i, e in enumerate(ent):
            ix = e & 0x3FF
            if e & 0x8000:
                prio = 0x8000 if bits[i // 16] >> (i % 16) & 1 else 0
                mB[ix] = prio | (e & 0x6000) | (pool + slotp[k]); k += 1
            if e & 0x1000:
                mA[ix] = (mB[ix] & 0x8000) | ((e & 0x0C00) << 3) | (pool + slotp[k]); k += 1
            else:
                mA[ix] = 0
        assert len(data) <= POOLMAX
        for j, t in enumerate(data):
            vram[pool + j] = tile(t, 0)
        for m, nt in rows:                                # row tiles go to that plane row's block of ROWK
            for j in range(nt):
                vram[vram_tile(m * ROWK + 1 + j)] = tile(b, o); o += 32
        out.append(render(pal, vram, mB, mA, vs))


HIDDEN = [0]                                              # cells whose plane A tile ended up under plane B


def render(pal, vram, mB, mA, vs=0):
    """Shadow/Highlight on: a cell is shown normally if either plane's tile has priority, otherwise shadowed.
    Builds the whole 256x256 plane and returns the window (screen lines 16-207 = plane lines + vs)."""
    luts = np.zeros((2, 4, 16, 3))
    for m, lv in enumerate((E.NORM, E.SHAD)):
        for l in range(4):
            for e in range(1, 16):
                w = pal[l * 16 + e]; luts[m, l, e] = (lv[(w >> 1) & 7], lv[(w >> 5) & 7], lv[(w >> 9) & 7])
    plane = np.zeros((256, 256, 3))
    def px(w):
        t = vram.get(w & 0x7FF, np.zeros((8, 8), np.uint8)) if (w & 0x7FF) else np.zeros((8, 8), np.uint8)
        if w & 0x800: t = t[:, ::-1]
        if w & 0x1000: t = t[::-1]
        return t, (w >> 13) & 3
    for ix in range(1024):
        if not (mB[ix] or mA[ix]):
            continue
        ty, tx = divmod(ix, 32)
        tb, lb_ = px(mB[ix]); ta, la_ = px(mA[ix])
        lut = luts[0 if (mB[ix] | mA[ix]) & 0x8000 else 1]
        blk = lut[lb_][tb]
        if (mA[ix] & 0x8000) >= (mB[ix] & 0x8000):          # a priority-1 plane B tile is drawn over a
            blk[ta > 0] = lut[la_][ta][ta > 0]               # priority-0 plane A tile (B has no see-through)
        elif (ta > 0).any():
            HIDDEN[0] += 1
        plane[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = blk
    lines = (np.arange(16, 208) + vs) % 256
    return np.clip(plane[lines, 16:240], 0, 255).astype(np.uint8)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != 'tone']
    clip = args[0]
    n = int(args[1]) if len(args) > 1 else 100000
    out = os.path.join(E.HERE, 'out'); os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f'{clip}_v3{"_tone" if TONE else ""}.bin')
    stream, frames, st = encode(clip, n, path)
    fb = np.array(st['frame_bytes']); ft = np.array(st['frame_tiles'])
    dur = len(frames) / E.FPS
    print(f'{clip}: {len(frames)} frames ({dur:.1f} s), {len(st["scene"])} scenes, stream {len(stream) / 1024:.0f} KB, '
          f'average {len(stream) / dur / 1024:.0f} KB/s')
    print('scenes (start, tiles, two-layer cells, record bytes):', st['scene'])
    print(f'frame bytes mean {fb.mean():.0f} max {fb.max()} | frame tiles mean {ft.mean():.0f} max {ft.max()}')
    imgs = simulate(stream)
    errs, shots = [], []
    for fi in range(0, len(frames), 6):
        f, new, bg, cel = frames[fi]
        ref = bg.copy()
        if cel is not None:
            ref[cel[1]] = cel[0][cel[1]]
        errs.append(E.delta_e(ref, imgs[fi]))
        if fi % 72 == 0:
            shots.append((ref.astype(np.uint8), imgs[fi]))
    print(f'preview delta-E vs scaled source: mean {np.mean(errs):.2f} max {np.max(errs):.2f}')
    c = Image.new('RGB', (E.WIN_W * 2, E.WIN_H * len(shots)))
    for r, (ref, img) in enumerate(shots):
        c.paste(Image.fromarray(ref), (0, r * E.WIN_H)); c.paste(Image.fromarray(img), (E.WIN_W, r * E.WIN_H))
    c.save(path.replace('.bin', '_preview.png'))

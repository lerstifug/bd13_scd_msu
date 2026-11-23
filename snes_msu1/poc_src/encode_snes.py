"""SNES MSU-1 prototype encoder: one Brain Dead 13 clip as a layered stream.  python encode_snes.py [BD04_011] [path]

The picture keeps the 3DO's shape: its 288x216 square pixels are 252x216 SNES pixels (8:7 wide), at screen
x 2-253 (a window blacks out the 2 columns each side) and lines 4-219.
Mode 1, two 4-bit layers that share 8 palettes of 15 colours (BGR555, fitted per scene):
  BG2  the scene background, a 64x32-tile map (a ring both ways, 512 x 256 pixels) scrolled in both directions.
       Each panorama tile in view has a slot in a pool of 1024 (VRAM tiles 0-1023); tiles coming into view go
       into free slots and their map entries.  A still scene is a pan that never moves.
  BG1  the cel layer: colour 0 is transparent, so the background shows through per pixel.  Cel tiles live in
       a ring of 831 slots (VRAM tiles 1025-1855, slot 0 = the empty tile): a frame reuses the tiles still there
       and writes its new ones in ring order around the ones on screen, so they are mostly one DMA.
The 3DO pans by sending the newly exposed strips (MVBG/MVB8) and moving the camera (MOVE).  A looping pan (the
camera jumps back by the panorama's period) is unwrapped into one long scroll through the repeated panorama.
Stream (<name>.msu): one packet per 60 Hz field, read by the player in the forced-blank letterbox lines:
  flags (bit0: keep the screen blank through the next field, bit7: end of stream), command count, commands:
  0 lo hi nlo nhi  VRAM word address, byte count: DMA from the MSU-1 data port to $2118/$2119 (step 1)
  1 a nlo nhi      CGRAM byte address a/2, byte count: DMA to $2122
  2 lo hi nlo nhi  VRAM as 0 but stepping 32 words: a map column
  3 x x y y        BG2 scroll: horizontal, vertical
  4 r v            PPU register $21r = v (BG2's map and the main screen layers, for the alternating buffers)
  each followed by its data.  A field shows the frame its packet completed from line 16 on.
A still scene too heavy for that (close-ups whose frame on screen and next frame need more cel tiles together
than the ring holds) is drawn whole instead: background and cels in one layer, alternating frame by frame
between BG2 and BG1, each frame switched in by one register write (only those scenes: one palette per tile for
both the background and cel pixels is coarser at cel edges).
A scene cut needs a new background while the old one is on screen, so it blanks the screen for a field or two
and sends the whole scene in the blank fields.
Audio (<name>-1.pcm): the clip's 3DO soundtrack at 44.1 kHz stereo.  Video follows the audio clock: frame f
becomes visible at field round(f * 60.0988 / 12), so the two drift apart by less than half a field.
"""
import copy, csv, json, os, pickle, struct, sys, wave
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..', '..')
SRC3 = os.path.join(ROOT, 'bd13', '3do', 'extracted')
BUILD = os.path.join(HERE, '..', 'build')
SHOTS = os.path.join(HERE, '..', 'screenshots')
os.makedirs(SHOTS, exist_ok=True)                      # (the encoders save their simulated pictures here)
sys.path.insert(0, os.path.join(ROOT, 'segacd'))
import scroll_src                                                   # the 3DO panorama (BACK + strips), camera

W, H = 256, 216                     # the screen width, the picture height (the 3DO's 216 lines)
PW, PX0 = 252, 2                    # the picture: the 3DO's 288 square pixels are 252 SNES pixels (8:7), at x 2
TW, TH = W // 8, H // 8             # 32 x 27 tiles
SX = PW / 288                       # 3DO pixels -> SNES pixels across (down: one line each)
PAD = 8                             # the panorama starts 8 pixels left of the 3DO's column 0
ROW0 = 1                            # map row of the picture's top: screen line 4 shows map line 8 (scroll 4)
FIELD_HZ = 21477272.7 / (1364 * 262 - 2)           # NTSC: 60.0988 fields per second
FPS = 12
BG_SLOTS = 1024                     # background tile pool: VRAM tiles 0-1023 (BG2's whole window)
BG_SWAP = 64                        # new background tiles a frame may write over tiles leaving the screen, in
                                    # the switch field itself (a 12 Hz pan: 4 columns a frame, 3.5 fit beside)
MAX_DEV = 160                       # a camera the pool would leave further than this from the 3DO's: buffers
LATE = 4                            # fields a heavy frame may switch late
WHOLE = {'BD51_001'}                # clips drawn whole from start to end, one layer and no cuts between scenes (the
                                    # Dragon's Lair II trailer: its scrolling scenes one after another were too heavy)
WHOLE_SIZE = (224, 192)             # their picture, centred on black: 28 x 24 tiles at most change in a frame, which
                                    # the fields of a frame carry (the full picture's 32 x 27 dropped frames in pans)


def shrink(a, nearest=False):
    """A screen-wide picture (H, W[, 3]) with the picture area (PW x H at PX0) at WHOLE_SIZE, centred, on black."""
    pw, ph = WHOLE_SIZE
    x0 = (PX0 + (PW - pw) // 2) // 8 * 8                           # (on tile edges: the border tiles stay empty)
    y0 = (H - ph) // 16 * 8
    src = a[:, PX0:PX0 + PW]
    out = np.zeros_like(a)
    if nearest:
        out[y0:y0 + ph, x0:x0 + pw] = np.array(Image.fromarray(src.astype(np.uint8) * 255).resize((pw, ph), Image.BOX)) > 127
        return out
    chans = [np.array(Image.fromarray(src[:, :, c].astype(np.float32), 'F').resize((pw, ph), Image.BOX))
             for c in range(src.shape[2])]
    out[y0:y0 + ph, x0:x0 + pw] = np.dstack(chans)
    return out
FIELD_SCROLL = False                # True: the background scrolls every field (60 Hz) between the 3DO positions;
                                    # False: once a frame (12 Hz), in even steps (the user wants the frame rate)
BUF_SLOTS = 959                     # alternating buffers: frame tiles in BG2 0-958 / BG1 1024-1982 (959: empty)
BUF_EMPTY = 959
BUF_MAP = (0x3C00, 0x7C00)          # their maps (BG2 at $3C00 in this mode, BG1 as always)
BUF_TM = (0x02, 0x01)               # the main screen shows BG2 only / BG1 only
MAX_LAG = 4                         # alternating buffers: a frame later than this is dropped
BUF_BGPAL = 3                       # alternating buffers: palettes for the background (the rest: per frame)
BAND = 12                           # map rows a band switches at most
CEL_BASE = 1024                     # BG1 character base (word $4000); slot 0 is the empty tile
CEL_SLOTS = 831                     # cel slots 1-831 (VRAM tiles 1025-1855)
MAP2, MAP1 = 0x7400, 0x7C00         # word addresses of the BG2 (64x32) and BG1 (32x32) maps
LINE_SHOW = 4                       # the picture's first screen line (player.asm LINE_SHOW)
PACKET = 6144                       # cost per normal packet (lines 220-261 and 0-3: about 7.5 KB of DMA)
PACKET_BLANK = 30000                # cost per blank packet (the field stays blank)
CMD_COST = 128                      # a command's CPU time (reading its header, setting up the DMA) in DMA bytes
CEL_W = 2.0                         # weight of cel pixels in the palette fit
DITHER = 0.018                      # ordered dither strength for backgrounds (OKLab units)

BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16 - 15 / 32
TILE_DITHER = np.tile(BAYER, (2, 2)).ravel()                       # the same for every tile (8 = 2 x 4)


# ---- colour --------------------------------------------------------------------------------------------------
def srgb2lin(a):
    a = np.asarray(a, np.float64) / 255
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def lin2srgb(a):
    a = np.clip(a, 0, 1)
    return 255 * np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)


M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929], [0.2119034982, 0.6806995451, 0.1073969566],
               [0.0883024619, 0.2817188376, 0.6299787005]])
M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468], [1.9779984951, -2.4285922050, 0.4505937099],
               [0.0259040371, 0.7827717662, -0.8086757660]])


def lin2lab(x):
    return np.cbrt(x @ M1.T) @ M2.T


def lab2lin(x):
    return (x @ np.linalg.inv(M2).T) ** 3 @ np.linalg.inv(M1).T


def snap555(lab):
    """OKLab colours -> the nearest BGR555 colours (as OKLab) and their 15-bit words."""
    s = np.clip(np.rint(lin2srgb(lab2lin(lab)) * 31 / 255), 0, 31).astype(int)
    word = s[..., 0] | s[..., 1] << 5 | s[..., 2] << 10
    return lin2lab(srgb2lin(s * 255 / 31)), word


def rgb555(word):
    w = np.asarray(word)
    return np.stack([w & 31, w >> 5 & 31, w >> 10 & 31], -1) * 255 // 31


# ---- source --------------------------------------------------------------------------------------------------
def resize_lin(a, mask=None, size=(W, H)):
    """sRGB (and an alpha) -> size, averaged in linear light (premultiplied for cels)."""
    lin = srgb2lin(a)
    al = np.ones(a.shape[:2]) if mask is None else mask
    ch = [np.array(Image.fromarray((lin[:, :, c] * al).astype(np.float32), 'F').resize(size, Image.BOX))
          for c in range(3)]
    A = np.array(Image.fromarray(al.astype(np.float32), 'F').resize(size, Image.BOX))
    return np.dstack(ch) / np.maximum(A, 1e-6)[:, :, None], A


def clip_dir(clip):
    for dn in ('1', '2'):
        d = os.path.join(SRC3, 'disc' + dn, clip)
        if os.path.isdir(d):
            return d, dn
    sys.exit(f'{clip}: not exported (bd13/3do/export_3do.py)')


def load_frames(clip, path):
    """The path's 3DO frames as (3DO frame number, new background?, cel (linear RGB, mask) or None, camera)."""
    d, dn = clip_dir(clip)
    rows = [r for r in csv.DictReader(open(os.path.join(SRC3, 'manifest.csv'))) if r['disc'] == dn and r['file'] == clip + '.DAT']
    idx = json.load(open(os.path.join(SRC3, 'logic_ALL.json')))[clip]['paths'][path]
    cam = json.load(open(os.path.join(d, 'camera.json')))
    cel = None
    out, cache = [], {}
    for i in idx:
        r = rows[i]
        f, ch = int(r['frame']), r['chunks'].split()
        new = any(t in ch and os.path.exists(os.path.join(d, f'{t}_{f:05d}.png')) for t in ('BACK', 'BAC8'))
        if new:
            cel = None
        for tag in ('CL16', 'CELL'):
            p = os.path.join(d, f'{tag}_{f:05d}.png')
            if tag in ch and os.path.exists(p):
                if p not in cache:
                    a = np.array(Image.open(p).convert('RGBA'))
                    rgb, A = resize_lin(a[:, :, :3], a[:, :, 3] / 255, size=(PW, H))
                    full, m = np.zeros((H, W, 3)), np.zeros((H, W), bool)
                    full[:, PX0:PX0 + PW], m[:, PX0:PX0 + PW] = rgb, A >= 0.5
                    cache[p] = (full, m)
                cel = cache[p]
        out.append((f, new, cel, tuple(cam[f])))
    return out


def panorama(clip, frames3, xs, ys):
    """A scene's background as one 3DO image (BACK and the strips the camera exposed, in any direction) and the
    camera in it.  Returns (image (h,w,3) sRGB, camera column, camera row per frame)."""
    img, left, top = scroll_src.virtual_bg2d(clip, min(frames3), max(frames3), set(frames3))
    x, y = np.array(xs) - left, np.array(ys) - top
    jumps = [(a, b) for a, b in zip(x, x[1:]) if abs(b - a) > 200]
    if not jumps:
        return img, x, y
    # a looping pan: the camera jumps back by the panorama's period p (where the image repeats itself)
    anchor = min(min(a, b) for a, b in jumps)
    im = img.astype(np.int32)
    best = min((np.abs(im[:, anchor:anchor + w] - im[:, anchor + p:anchor + p + w]).mean(), p)
               for p in range(200, img.shape[1] - anchor - 60) for w in [min(288, img.shape[1] - anchor - p)])
    p = best[1]
    xu = [int(x[0])]
    for v in x[1:]:
        xu.append(min((int(v) + k * p for k in range(-40, 41)), key=lambda c: abs(c - xu[-1])))
    xu = np.array(xu)
    # the seam: the column where the image best matches itself one period earlier
    col = np.abs(im[:, p:] - im[:, :-p]).mean((0, 2))
    seam = p + int(np.convolve(col, np.ones(8), 'valid').argmin())
    left = max(0, -int(xu.min()))                                   # (a camera that wraps leftwards past column 0)
    xu = xu + left
    src = np.arange(int(xu.max()) + 288 + 16) - left
    src = np.where((src >= 0) & (src < seam), src, seam - p + (src - seam) % p)
    print(f'  looping pan: period {p} px (mean difference {best[0]:.1f}), seam at {seam}, camera 0..{xu.max()}')
    return img[:, src], xu, y


def view_cells(sx, sy):
    """The panorama tiles (column, row) the picture shows with its top left corner at (sx, sy)."""
    return [(c, r) for c in range(sx // 8, (sx + PW - 1) // 8 + 1) for r in range(sy // 8, (sy + H - 1) // 8 + 1)]


def even_steps(sx, sy):
    """The camera moving every frame: the 3DO mostly moves it every other frame by twice the step (a 6 Hz scroll,
    BD03_003), so a one-frame hold inside a move becomes the halfway point.  A longer hold still stops it."""
    pos = list(zip(sx, sy))
    n = len(pos)
    if n < 3:
        return list(sx), list(sy)
    keys = [j for j in range(n) if j == 0 or j == n - 1 or pos[j] != pos[j - 1] or pos[j + 1] == pos[j]]
    out = list(pos)
    for ka, kb in zip(keys, keys[1:]):
        for j in range(ka + 1, kb):
            t = (j - ka) / (kb - ka)
            out[j] = tuple(int(round(pos[ka][d] + (pos[kb][d] - pos[ka][d]) * t)) for d in (0, 1))
    return [p[0] for p in out], [p[1] for p in out]


def limit_steps(sx, sy):
    """The camera with no step bigger than the background tile pool can take: the tiles on screen and the next
    frame's together must fit BG_SLOTS.  A bigger step starts in the frames before it (the 3DO mostly holds the
    camera for two frames between moves, so this fills in a held frame; the cels stay where the 3DO draws them).
    Returns (sx, sy, how far the camera moved from the 3DO's at most)."""
    fits = lambda a, b: len(set(view_cells(sx[a], sy[a])) | set(view_cells(sx[b], sy[b]))) <= BG_SLOTS + BG_SWAP
    ox, oy = list(sx), list(sy)
    sx, sy = list(sx), list(sy)
    moved = 0
    for i in range(1, len(sx)):
        k = i
        while k > 0 and not fits(k - 1, k):
            while not fits(k - 1, k):
                for s in (sx, sy):                                  # a tile closer, on each axis that moves
                    d = s[k] - s[k - 1]
                    if d:
                        s[k - 1] += int(np.sign(d)) * min(8, abs(d))
                moved += 1
            k -= 1                                                  # (then the step before it, which grew)
    dev = max(max(abs(a - b) for a, b in zip(ox, sx)), max(abs(a - b) for a, b in zip(oy, sy)))
    if moved:
        print(f'  camera moved in {moved} steps to fit the background tiles (at most {dev} px from the 3DO)')
        STATS['steps_moved'] = STATS.get('steps_moved', 0) + moved
    return sx, sy, dev


def tiles(a):
    """(216,256,...) -> (864,64,...) in map order."""
    return a.reshape(TH, 8, TW, 8, *a.shape[2:]).swapaxes(1, 2).reshape(TH * TW, 64, *a.shape[2:])


def untile(t):
    return t.reshape(TH, TW, 8, 8, *t.shape[2:]).swapaxes(1, 2).reshape(H, W, *t.shape[2:])


# ---- palettes ------------------------------------------------------------------------------------------------
def kmeans(X, w, k, it=12, seed=0):
    rng = np.random.default_rng(seed)
    if len(X) <= k:
        return np.concatenate([X, np.repeat(X[-1:], k - len(X), 0)]) if len(X) else np.zeros((k, 3))
    c = [X[rng.choice(len(X), p=w / w.sum())]]
    for _ in range(k - 1):                                          # k-means++
        d = ((X[:, None] - np.array(c)[None]) ** 2).sum(2).min(1) * w
        c.append(X[rng.choice(len(X), p=d / d.sum())] if d.sum() > 0 else X[0])
    c = np.array(c)
    for _ in range(it):
        j = ((X[:, None] - c[None]) ** 2).sum(2).argmin(1)
        for q in range(k):
            m = j == q
            if m.any():
                c[q] = (X[m] * w[m, None]).sum(0) / w[m].sum()
    return c


def tile_errors(T, M, pals):
    """T (n,64,3), M (n,64) opaque, pals (8,15,3) -> (n,8) squared error with each palette."""
    e = np.empty((len(T), len(pals)))
    for p, P in enumerate(pals):
        for s in range(0, len(T), 4096):
            e[s:s + 4096, p] = (((T[s:s + 4096, :, None] - P[None, None]) ** 2).sum(3).min(2) * M[s:s + 4096]).sum(1)
    return e


def fit_palettes(T, M, wt, rounds=5, npal=8):
    """npal palettes of 15 colours for the tiles T (OKLab, (n,64,3)) with opaque masks M and weights wt."""
    mean = (T * M[:, :, None]).sum(1) / np.maximum(M.sum(1), 1)[:, None]
    grp = ((mean[:, None] - kmeans(mean, wt * M.sum(1) + 1e-9, npal)[None]) ** 2).sum(2).argmin(1)
    rng = np.random.default_rng(1)
    for _ in range(rounds):
        pals = []
        for g in range(npal):
            sel = np.where(grp == g)[0]
            X = T[sel][M[sel]]
            wx = np.repeat(wt[sel], M[sel].sum(1))
            if len(X) > 40000:
                k = rng.choice(len(X), 40000, replace=False, p=wx / wx.sum())
                X, wx = X[k], np.ones(len(k))
            pals.append(kmeans(X, wx, 15) if len(X) else np.zeros((15, 3)))
        pals = np.array(pals)
        grp = tile_errors(T, M, pals).argmin(1)
    return snap555(pals)


def quantise(T, M, pals, dither):
    """Tiles -> (palette per tile, colour indices 1-15 per pixel, 0 where not opaque)."""
    pal = tile_errors(T, M, pals).argmin(1)
    idx = np.empty(T.shape[:2], np.uint8)
    for s in range(0, len(T), 4096):
        dw = dither if np.isscalar(dither) else dither[s:s + 4096, :, None]
        t = T[s:s + 4096] + TILE_DITHER[None, :, None] * dw
        idx[s:s + 4096] = ((t[:, :, None] - pals[pal[s:s + 4096]][:, None]) ** 2).sum(3).argmin(2) + 1
    return pal, np.where(M, idx, 0).astype(np.uint8)


def planar(px):
    """(n,64) colour indices -> n 32-byte SNES 4-bit tiles (rows of bitplanes 0/1, then 2/3)."""
    p = np.asarray(px, np.uint8).reshape(-1, 8, 8)
    planes = [np.packbits((p >> b) & 1, axis=2)[:, :, 0] for b in range(4)]          # (n,8) each
    lo = np.stack([planes[0], planes[1]], 2).reshape(-1, 16)
    hi = np.stack([planes[2], planes[3]], 2).reshape(-1, 16)
    return [bytes(r) for r in np.concatenate([lo, hi], 1)]


# ---- stream --------------------------------------------------------------------------------------------------
def cmd_vram(addr, data, step32=False):
    return [bytes([2 if step32 else 0, addr & 255, addr >> 8, len(data) & 255, len(data) >> 8]) + data]


def cmd_cgram(addr, data):
    return [bytes([1, addr, len(data) & 255, len(data) >> 8]) + data]


def cmd_reg(reg, value):
    """A PPU register $21reg."""
    return [bytes([4, reg, value])]


def cmd_scroll(x, y):
    """BG2's scroll: horizontal x, vertical y (screen line l shows BG2 line l + y)."""
    return [bytes([3, x & 255, (x >> 8) & 3, y & 255, (y >> 8) & 3])]


def gradient_of(pano):
    """A still 3DO background (sRGB (216,288,3)) that is a plain vertical gradient (the revive clips, black
    backgrounds): its line colours as BGR555 words (216), else None (scroll_src.gradient_lines)."""
    if np.asarray(pano).shape[0] != H:
        return None
    g = scroll_src.gradient_lines(pano)
    if g is None:
        return None
    rgb = np.clip(np.round(g / 255 * 31), 0, 31).astype(int)
    return rgb[:, 0] | rgb[:, 1] << 5 | rgb[:, 2] << 10


def hdma_runs(vals, width):
    """An HDMA table (non-repeat entries, at most 127 lines each, ending in 0) writing vals (one per line, each
    width bytes) once per run of equal values."""
    out, k = bytearray(), 0
    while k < len(vals):
        n = 1
        while k + n < len(vals) and n < 127 and vals[k + n] == vals[k]:
            n += 1
        out += bytes([n]) + bytes(vals[k])
        k += n
    return bytes(out) + bytes(1)


def cmd_gradient(grad):
    """5: the backdrop gradient, or off (grad None).  The player adds the fixed colour ($2132) to the black
    backdrop by colour math; two HDMA channels set it on every line: channel 6 red and green (two writes), channel 7
    blue.  Lines 0-3 and 220-223 stay black."""
    if grad is None:
        return [bytes([5, 0, 0, 0, 0])]
    lines = [0] * LINE_SHOW + [int(w) for w in grad] + [0] * (224 - LINE_SHOW - H)
    rg = [(0x20 | w & 31, 0x40 | w >> 5 & 31) for w in lines]
    b = [(0x80 | w >> 10 & 31,) for w in lines]
    ta, tb = hdma_runs(rg, 2), hdma_runs(b, 1)
    return [bytes([5, len(ta) & 255, len(ta) >> 8, len(tb) & 255, len(tb) >> 8]) + ta + tb]


def words(entries):
    return np.asarray(entries, '<u2').tobytes()


def edges():
    """The empty cel tile (slot 0) and the cel map's rows outside the picture (BG2's map is a ring: all of it
    is background, and the lines outside the picture are blanked)."""
    top, bottom = ROW0 * 32, (32 - ROW0 - TH) * 32
    return (cmd_vram(CEL_BASE * 16, bytes(32)) + cmd_vram(MAP1, words([0] * top))
            + cmd_vram(MAP1 + (ROW0 + TH) * 32, words([0] * bottom)))


def edges_buffers():
    """The alternating buffers' empty tiles and their map rows outside the picture."""
    top, bottom = ROW0 * 32, (32 - ROW0 - TH) * 32
    out = cmd_vram(BUF_EMPTY * 16, bytes(32)) + cmd_vram((CEL_BASE + BUF_EMPTY) * 16, bytes(32))
    for base in BUF_MAP:
        out += cmd_vram(base, words([BUF_EMPTY] * top)) + cmd_vram(base + (ROW0 + TH) * 32, words([BUF_EMPTY] * bottom))
    return out


def size(cmds):
    """The time a list of commands takes, in DMA bytes."""
    return sum(len(c) + CMD_COST for c in cmds)


def merge_runs(cmds):
    """VRAM writes (step 1) that continue each other -> one command."""
    out = []
    for c in cmds:
        if out and out[-1][0] == 0 and c[0] == 0:
            pa, pn = out[-1][1] | out[-1][2] << 8, out[-1][3] | out[-1][4] << 8
            if pa + pn // 2 == (c[1] | c[2] << 8) and pn + len(c) - 5 < 65536:
                d = out[-1][5:] + c[5:]
                out[-1] = bytes([0, pa & 255, pa >> 8, len(d) & 255, len(d) >> 8]) + d
                continue
        out.append(c)
    return out


def fill(packets, caps, cmds):
    """Commands into the fields (field, budget) in order; VRAM runs (step 1) are split at the end of a field."""
    for c in cmds:
        for f, cap in caps:
            room = cap - size(packets[f])
            if size([c]) <= room:
                packets[f].append(c)
                break
            if c[0] == 0 and room > CMD_COST + 64 and len(c) > 64:   # split a long VRAM run
                k = (room - 5 - CMD_COST) // 32 * 32
                a, n = c[1] | c[2] << 8, len(c) - 5
                packets[f].append(bytes([0, a & 255, a >> 8, k & 255, k >> 8]) + c[5:5 + k])
                a += k // 2
                c = bytes([0, a & 255, a >> 8, (n - k) & 255, (n - k) >> 8]) + c[5 + k:]
        else:
            sys.exit('a frame does not fit its fields')


# ---- encoder -------------------------------------------------------------------------------------------------
class Scene:
    """One background: the panorama in SNES tiles (columns x rows), its palettes, and each frame's cels."""

    def __init__(self, clip, frames):
        f3 = [f[0] for f in frames]
        self.clip, self.f3 = clip, f3
        xs, ys = [f[3][0] for f in frames], [f[3][1] for f in frames]
        still = len(set(zip(xs, ys))) == 1
        pano, xu, yv = panorama(clip, f3[:1] if still else f3, xs, ys)   # a still scene: its BACK only
        self.grad = gradient_of(pano) if still else None            # a plain vertical gradient: HDMA, no tiles
        lin = resize_lin(pano, size=(round(pano.shape[1] * SX), pano.shape[0]))[0]   # (down: one line each)
        sw = int(np.ceil((PAD + lin.shape[1] + 8) / 8)) * 8
        sh = int(np.ceil((lin.shape[0] + 8) / 8)) * 8
        lin = np.pad(lin, ((0, sh - lin.shape[0]), (PAD, sw - PAD - lin.shape[1]), (0, 0)), mode='edge')
        self.ncol, self.nrow = sw // 8, sh // 8
        # the camera: the panorama pixel at the picture's top left corner (SNES pixels), as the 3DO has it, and
        # as the background tile pool can follow it
        self.sx0, self.sy0 = [PAD + int(round(v * SX)) for v in xu], [int(v) for v in yv]
        self.sx, self.sy, self.dev = limit_steps(*((self.sx0, self.sy0) if FIELD_SCROLL else even_steps(self.sx0, self.sy0)))
        # tiles in (column, row) order
        T = lin2lab(lin.reshape(self.nrow, 8, self.ncol, 8, 3).transpose(2, 0, 1, 3, 4).reshape(self.ncol, self.nrow, 64, 3))
        # cels: each distinct tile once, weighted by how often it shows
        self.cel_keys, cel_tiles = [], {}
        for f in frames:
            keys = [None] * (TW * TH)
            if f[2] is not None:
                rgb, m = tiles(f[2][0]), tiles(f[2][1])
                for t in np.where(m.any(1))[0]:
                    k = (rgb[t].tobytes(), m[t].tobytes())
                    if k not in cel_tiles:
                        cel_tiles[k] = [lin2lab(rgb[t]), m[t], 0]
                    cel_tiles[k][2] += 1
                    keys[t] = k
            self.cel_keys.append(keys)
        ck = list(cel_tiles)
        Tc = np.array([cel_tiles[k][0] for k in ck]).reshape(-1, 64, 3)
        Mc = np.array([cel_tiles[k][1] for k in ck], bool).reshape(-1, 64)
        wc = CEL_W * np.sqrt([cel_tiles[k][2] for k in ck]) if ck else np.zeros(0)
        # the palette fit sees each background tile the camera shows once
        shown = sorted({cr for s, r in zip(self.sx, self.sy) for cr in view_cells(s, r)})
        Ts = T[tuple(np.array(shown).T)]
        if self.grad is not None:                                   # the palettes are the cels' alone
            Ts = Ts[:0]
            if not ck:
                Tc, Mc, wc = np.zeros((1, 64, 3)), np.ones((1, 64), bool), np.ones(1)
        pals, self.words = fit_palettes(np.concatenate([Ts, Tc]), np.concatenate([np.ones(Ts.shape[:2], bool), Mc]),
                                        np.concatenate([np.ones(len(Ts)), wc]))
        bpal, bpx = quantise(T.reshape(-1, 64, 3), np.ones((self.ncol * self.nrow, 64), bool), pals, DITHER)
        self.bpal = bpal.reshape(self.ncol, self.nrow)
        self.bpx = bpx.reshape(self.ncol, self.nrow, 64)
        cpal, cpx = quantise(Tc, Mc, pals, 0) if ck else (np.zeros(0, int), np.zeros((0, 64), np.uint8))
        data = planar(cpx) if ck else []
        self.cel_of = {k: (int(cpal[j]), data[j], cpx[j]) for j, k in enumerate(ck)}
        self.n_bg, self.n_cel = len(Ts), len(ck)
        self.lin, self.cels = lin, [f[2] for f in frames]

    def needs_buffers(self):
        """Drawn whole, in the two alternating buffers: a still scene whose frame on screen and next frame need
        more cel tiles together than the ring holds (heavy close-ups), or a camera too fast for the background
        tile pool (it would end up more than MAX_DEV pixels from where the 3DO puts it)."""
        if self.dev > MAX_DEV:
            return True
        if len(set(zip(self.sx, self.sy))) > 1:
            return False
        sets = [{self.cel_of[k][1] for k in keys if k is not None} for keys in self.cel_keys]
        return any(len(x | y) > CEL_SLOTS for x, y in zip(sets, sets[1:]))

    def field_track(self, fl):
        """The background camera for every field of a moving scene (fl: its frames' fields): through the 3DO's
        camera positions, linear in time between them, so the background scrolls a little every field instead of
        jumping once a frame.  A one-frame hold inside a move (the 3DO mostly moves every other frame) is passed
        through, so a pan keeps one speed; a longer hold stops it.  Returns {field: (sx, sy)}, or None when two
        fields' views together would not fit the background tile pool (then the camera stays per frame)."""
        pos = list(zip(self.sx0, self.sy0))
        n = len(pos)
        if not FIELD_SCROLL or n < 2 or len(set(pos)) == 1 or self.grad is not None:
            return None
        keys = [j for j in range(n) if j == 0 or j == n - 1 or pos[j] != pos[j - 1]
                or pos[j + 1] == pos[j]]                            # (moves, and frames of a real stop)
        track = {}
        for ka, kb in zip(keys, keys[1:]):
            fa, fb = fl[ka], fl[kb]
            for f in range(fa, fb + 1):
                t = (f - fa) / max(1, fb - fa)
                track[f] = tuple(int(round(pos[ka][d] + (pos[kb][d] - pos[ka][d]) * t)) for d in (0, 1))
        end = pos[-1]
        for f in range(fl[-1] + 1, fl[-1] + 4 * LATE + 64):         # (late frames: held at the last position)
            track[f] = end
        fs = sorted(track)
        for f0, f1 in zip(fs, fs[1:]):
            if track[f0] != track[f1] and len(set(view_cells(*track[f0])) | set(view_cells(*track[f1]))) > BG_SLOTS:
                return None
        return track

    def tile(self, c, r):
        """Panorama tile (c, r): its data and palette."""
        return planar(self.bpx[c, r][None])[0], int(self.bpal[c, r])

    def picture(self, i, cam=None):
        """Frame i as BGR555 words (what the SNES should show; black outside the picture's 252 columns), its
        background at camera cam (sx, sy) when given (a field between frames)."""
        if self.grad is not None:
            img = np.repeat(self.grad[:, None], W, 1).astype(np.int64)
            keys = self.cel_keys[i]
            cimg = untile(np.array([self.cel_of[k][2] if k is not None else np.zeros(64, np.uint8) for k in keys]))
            cpl = untile(np.repeat(np.array([self.cel_of[k][0] if k is not None else 0 for k in keys])[:, None], 64, 1))
            m = cimg > 0
            img[m] = self.words[cpl[m], cimg[m] - 1]
            return window(img)
        s, y = (cam[0] if cam else self.sx[i]) - PX0, cam[1] if cam else self.sy[i]   # the pixel at screen (0, line 4)
        cols = np.arange(s // 8, (s + W - 1) // 8 + 1)
        rows = np.arange(y // 8, (y + H - 1) // 8 + 1)
        px = self.bpx[cols][:, rows].reshape(len(cols), len(rows), 8, 8).transpose(1, 2, 0, 3).reshape(len(rows) * 8, -1)
        pl = np.repeat(np.repeat(self.bpal[cols][:, rows].T, 8, 0), 8, 1)
        img = self.words[pl, px - 1][y % 8:y % 8 + H, s % 8:s % 8 + W]
        keys = self.cel_keys[i]
        cimg = untile(np.array([self.cel_of[k][2] if k is not None else np.zeros(64, np.uint8) for k in keys]))
        cpl = untile(np.repeat(np.array([self.cel_of[k][0] if k is not None else 0 for k in keys])[:, None], 64, 1))
        m = cimg > 0
        img[m] = self.words[cpl[m], cimg[m] - 1]
        return window(img)


def window(img):
    """The SNES window: black (the backdrop, CGRAM 0) left and right of the picture."""
    img = img.astype(np.uint16)
    img[:, :PX0] = 0
    img[:, PX0 + PW:] = 0
    return img


class BgRing:
    """The background tile pool (BG2, VRAM tiles 0-1023) and its 64x32 map, a ring both ways (512 x 256
    pixels).  Each panorama tile in view has a slot; a frame's new tiles go in the longest runs of free slots
    (not the ones on screen), and their map entries in runs of a map column or row, whichever is fewer."""

    def __init__(self):
        self.slot_of, self.content, self.shown = {}, [None] * BG_SLOTS, set()
        self.owner = {}                                             # map position -> the tile its entry shows

    def frame(self, sc, view, i):
        """The commands that show view: (before the switch, in the switch field).  New tiles go in free slots
        before the switch; when those run out, the rest (up to BG_SWAP) go over tiles leaving the screen, which
        are on screen until the switch, so they are written in the switch field with the scroll."""
        mine = {self.slot_of[c] for c in view if c in self.slot_of}
        keep = self.shown | mine
        new = [c for c in view if c not in self.slot_of]
        pos = lambda cr: (cr[0] % 64, cr[1] % 32)
        spare = BG_SLOTS - len(keep)
        leaving = sorted(self.shown - mine)
        if len(new) > spare + min(BG_SWAP, len(leaving)):
            sys.exit(f'frame {i}: {len(keep)} background tiles kept + {len(new)} new do not fit')
        free = CelRing.free_slots(keep, range(BG_SLOTS))
        up, late, ent = [], [], {}
        for k, cr in enumerate(new):
            q = next(free) if k < spare else leaving[k - spare]
            if self.content[q] is not None:
                del self.slot_of[self.content[q]]
            self.content[q], self.slot_of[cr] = cr, q
            data, pal = sc.tile(*cr)
            (up if k < spare else late).extend(cmd_vram(q * 16, data))
            ent[cr] = q | pal << 10
        for cr in view:                                             # still in VRAM, but its map entry was taken by
            if cr not in ent and self.owner.get(pos(cr)) != cr:     # a tile a ring's width away
                ent[cr] = self.slot_of[cr] | int(sc.bpal[cr]) << 10
        for cr in ent:
            self.owner[pos(cr)] = cr
        self.shown = {self.slot_of[c] for c in view}
        return merge_runs(up) + map_runs(ent), merge_runs(late)


def map_runs(ent):
    """BG2 map entries {(column, row): word} -> commands: runs down a map column (step 32) or along a row of one
    32-column screen, whichever needs fewer."""
    at = lambda c, r: MAP2 + (c % 64 // 32) * 0x400 + (r % 32) * 32 + c % 32
    def runs(key, nxt):
        out, cur = [], []
        for cr in sorted(ent, key=key):
            if cur and nxt(cur[-1]) == cr:
                cur.append(cr)
            else:
                cur and out.append(cur)
                cur = [cr]
        cur and out.append(cur)
        return out
    down = runs(lambda cr: (cr[0], cr[1]), lambda cr: (cr[0], cr[1] + 1))
    across = runs(lambda cr: (cr[1], cr[0]), lambda cr: (cr[0] + 1, cr[1]))
    down = [r2 for r in down for r2 in split(r, lambda a, b: b[1] % 32 == 0)]           # (the ring's wrap)
    across = [r2 for r in across for r2 in split(r, lambda a, b: b[0] % 32 == 0)]       # (the next screen)
    if len(down) <= len(across):
        return [c for r in down for c in cmd_vram(at(*r[0]), words([ent[cr] for cr in r]), step32=True)]
    return [c for r in across for c in cmd_vram(at(*r[0]), words([ent[cr] for cr in r]))]


def split(run, cut):
    """A run -> pieces, cut before each element where cut(previous, element)."""
    out = [[run[0]]]
    for a, b in zip(run, run[1:]):
        if cut(a, b):
            out.append([b])
        else:
            out[-1].append(b)
    return out


class NoFit(Exception):
    pass


class CelRing:
    """The cel tile slots.  A frame reuses the tiles still in VRAM; its new tiles go in the longest runs of free
    slots, so they mostly form one run (one DMA).  The frame's map rows normally all change in its own
    field; when the frame on screen and the new one do not fit together (close-ups: up to 704 tiles a frame), the
    rows switch in bands, top down, in the fields before (each band frees the old frame's tiles for the next)."""

    def __init__(self):
        self.content, self.where = [None] * (CEL_SLOTS + 1), {}
        self.rows = [set() for _ in range(TH)]                      # the slots each map row on screen uses
        self.rolled = 0                                             # frames that switched in bands

    @staticmethod
    def free_slots(protect, slots=range(1, CEL_SLOTS + 1)):
        """The slots not in use, longest runs first (each run is one DMA)."""
        runs, cur = [], []
        for q in slots:
            if q in protect:
                if cur:
                    runs.append(cur)
                cur = []
            else:
                cur.append(q)
        if cur:
            runs.append(cur)
        return iter([q for r in sorted(runs, key=len, reverse=True) for q in r])

    def transition(self, cells, caps, cut, i):
        """cells: per map cell (row-major) (tile data, palette) or None.  caps: [(field, room)] from the first
        field after the frame on screen to the frame's own field.  Returns {field: commands}."""
        if cut:                                                     # the screen is blank: nothing is on screen
            self.rows = [set() for _ in range(TH)]
        need = [list(dict.fromkeys(c[0] for c in cells[r * TW:(r + 1) * TW] if c is not None)) for r in range(TH)]
        slot, pending, out, rolled = {}, list(range(TH)), {}, False
        for r in pending:                                           # the tiles still in VRAM: kept from the start
            for d in need[r]:
                if d in self.where:
                    slot[d] = self.where[d]
        for t, (f, room) in enumerate(caps):
            last = t == len(caps) - 1
            protect = set().union(*self.rows) | set(slot.values())
            free = self.free_slots(protect)
            budget = room - CMD_COST - 64 * (len(pending) if last else min(BAND, len(pending)))
            runs, cost, blocked = [], 0, None
            for r in pending:
                for d in need[r]:
                    if d in slot:
                        continue
                    q = next((q for q in free if q not in protect), None)
                    if q is None:
                        blocked = 'room'
                        break
                    c = 32 + (0 if runs and runs[-1][0] == q - 1 else CMD_COST)
                    if cost + c > budget:
                        blocked = 'time'
                        break
                    if self.content[q] is not None:
                        del self.where[self.content[q]]
                    self.content[q], self.where[d], slot[d] = d, q, q
                    protect.add(q)
                    runs.append((q, d))
                    cost += c
                if blocked:
                    break
            cmds = merge_runs([c for q, d in runs for c in cmd_vram((CEL_BASE + q) * 16, d)])
            done = [r for r in pending if all(d in slot for d in need[r])]
            if last:
                if len(done) != len(pending):
                    raise NoFit(f'frame {i}: its cel tiles do not fit the fields before it')
                switch = pending
            elif blocked == 'room' or still_new(need, pending, slot, self.where) > CEL_SLOTS - len(protect):
                # the rest will not fit beside what is on screen: switch the finished rows (frees their old tiles)
                switch = []
                for r in pending:
                    if r not in done or len(switch) == BAND:
                        break
                    switch.append(r)
                if not switch:
                    raise NoFit(f'frame {i}: its cel tiles do not fit, even band by band')
            else:
                switch = []
            rolled = rolled or bool(switch and not last)
            for r in switch:
                self.rows[r] = {slot[d] for d in need[r]}
            pending = [r for r in pending if r not in switch]
            for a, b in row_runs(switch):
                ent = [0 if c is None else slot[c[0]] | c[1] << 10 for c in cells[a * TW:(b + 1) * TW]]
                cmds += cmd_vram(MAP1 + (ROW0 + a) * 32, words(ent))
            out[f] = cmds
        self.rolled += rolled
        return out


def still_new(need, pending, slot, where):
    """How many tiles the pending rows still need uploaded."""
    return len({d for r in pending for d in need[r] if d not in slot and d not in where})


def row_runs(rows):
    """Sorted rows -> (first, last) of each run of consecutive rows."""
    out = []
    for r in rows:
        if out and out[-1][1] == r - 1:
            out[-1][1] = r
        else:
            out.append([r, r])
    return out


def buffered_scene(sc, a, b, fields, packets, blank, shown, shown_at, last, grad_on=False, cont=None, whole=False):
    """A scene drawn whole (background and cels in one layer), alternating frame by frame between BG2 and BG1:
    the frame on screen is never touched, the next one fills the other layer, and one register write (TM, the
    main screen layers) shows it.  For heavy still close-ups and cameras too fast for the layered background;
    the camera is the 3DO's.  Each tile has one palette for its background and cel pixels, so this is only for
    the scenes the layered way cannot show whole.  whole (WHOLE units): a gradient is drawn as tiles too; cont (the
    state this returned for the scene before, in a WHOLE unit): no cut, the scene goes on from the last one's buffers
    with no blank fields, its background palettes written with its first frame's switch.  Returns (last field, late
    frames, state)."""
    pos = list(zip(sc.sx0, sc.sy0))
    view = lambda p: sc.lin[p[1]:p[1] + H, p[0]:p[0] + PW]
    keys, tile_of = [], {}                                          # per frame: its cells' keys; key -> source
    grad = sc.grad is not None and not whole                        # a gradient: cels only, the backdrop shows elsewhere
    full = lambda p: np.pad(view(p), ((0, 0), (PX0, W - PW - PX0), (0, 0)), mode='edge')
    if whole and len(set(pos)) > 1:                                 # a moving camera: each frame's background from
        vb, left, top = scroll_src.virtual_bg2d(sc.clip, sc.f3[0], sc.f3[-1], set(sc.f3))   # the 3DO's virtual
        track = scroll_src.camera_track(sc.clip, sc.f3[0], sc.f3[-1])                       # background (as the
        pos = [track.get(f, (0, 0)) for f in sc.f3]                                         # Sega CD), not the
                                                                                            # panorama (holes)
        def full(p):
            x0 = int(np.clip(p[0] - left, 0, vb.shape[1] - 288))
            y0 = int(np.clip(p[1] - top, 0, vb.shape[0] - 216))
            v = resize_lin(vb[y0:y0 + 216, x0:x0 + 288], size=(PW, H))[0]
            return np.pad(v, ((0, 0), (PX0, W - PW - PX0), (0, 0)), mode='edge')
    for j, cel in enumerate(sc.cels):
        img = np.zeros((H, W, 3))
        if whole:                                                   # smaller, on black
            img = shrink(full(pos[j]))
        elif not grad:
            img[:, PX0:PX0 + PW] = view(pos[j])
        m = np.zeros((H, W), bool)
        if cel is not None:
            m = shrink(cel[1], True) if whole else cel[1]
            img[m] = (shrink(cel[0]) if whole else cel[0])[m]
        T, M = tiles(img), tiles(m)
        ks = []
        for c in range(TW * TH):
            k = T[c].tobytes() + M[c].tobytes()             # (with its mask: a cel tile that looks like a background
                                                            # tile elsewhere, say plain black, must stay a cel tile)
            if k not in tile_of:
                tile_of[k] = [T[c], M[c], 0]
            tile_of[k][2] += 1
            ks.append(k)
        keys.append(ks)
    # palettes: BUF_BGPAL fitted once to the background, for the cells without cel pixels (so those tiles stay
    # the same from frame to frame and stay in VRAM); the rest fitted per frame to its cel cells, which may use
    # all 8 (written with the layer switch, so the frame on screen keeps its own)
    nb = 0 if grad else BUF_BGPAL                                   # (a gradient: all 8 palettes for the cels)
    bgt = {p: lin2lab(tiles(shrink(full(p)) if whole else full(p))) for p in dict.fromkeys(pos)}
    bgq = {}
    if grad:                                                        # empty tiles: the backdrop's gradient shows
        pb, wb = np.zeros((0, 15, 3)), np.zeros((0, 15), int)
        for p in bgt:
            bgq[p] = ([bytes(32)] * (TW * TH), np.zeros(TW * TH, int), np.zeros((TW * TH, 64), np.uint8))
    else:
        some = list(bgt.values())[::max(1, len(bgt) // 16)]         # (a moving camera: a sample of its views)
        Tb = np.concatenate(some)
        pb, wb = fit_palettes(Tb, np.ones(Tb.shape[:2], bool), np.ones(len(Tb)), npal=BUF_BGPAL)
        for p, T in bgt.items():
            bpal, bidx = quantise(T, np.ones(T.shape[:2], bool), pb, DITHER)
            bgq[p] = (planar(bidx), bpal, bidx)
    q, cgs = {}, []
    for j, ks in enumerate(keys):
        celc = [c for c in range(TW * TH) if tile_of[ks[c]][1].any()]
        uk = list(dict.fromkeys(ks[c] for c in celc))
        if uk:
            Tl = lin2lab(np.array([tile_of[k][0] for k in uk]))
            Mc = np.array([tile_of[k][1] for k in uk])
            wt = np.where(Mc.any(1), CEL_W, 1.0) * np.array([ks.count(k) for k in uk]) ** 0.5
            pf, wf = fit_palettes(Tl, Mc if grad else np.ones(Tl.shape[:2], bool), wt, npal=8 - nb)
        else:
            pf, wf = np.zeros((8 - nb, 15, 3)), np.zeros((8 - nb, 15), int)
        pals, wds = np.concatenate([pb, pf]), np.concatenate([wb, wf])
        if uk and grad:
            pal, idx = quantise(Tl, Mc, pals, 0)                    # cel pixels only (0: transparent)
            data = planar(idx)
            fq = {k: (data[n], int(pal[n]), idx[n]) for n, k in enumerate(uk)}
        elif uk:
            pal, idx = quantise(Tl, np.ones(Tl.shape[:2], bool), pals, DITHER * ~Mc)   # dither background pixels
            data = planar(idx)
            fq = {k: (data[n], int(pal[n]), idx[n]) for n, k in enumerate(uk)}
        cs = set(celc)
        bdata, bpal, bidx = bgq[pos[j]]
        q[j] = [fq[ks[c]] if c in cs else (bdata[c], int(bpal[c]), bidx[c]) for c in range(TW * TH)]
        cgs.append((wds, b''.join(struct.pack('<H', 0) + words(wds[p]) for p in range(nb, 8))))
    cg0 = b''.join(struct.pack('<H', 0) + words(wb[p]) for p in range(nb))
    print(f'  drawn whole in alternating buffers ({nb} background palettes, {8 - nb} per frame'
          + (', over the backdrop gradient)' if grad else ')'))
    held = cont['held'] if cont else [{}, {}]                       # per buffer: tile data -> slot
    shows = cont['shows'] if cont else 0
    lates, dropped = 0, 0
    for i in range(a, b):
        j, buf = i - a, shows % 2
        cells = q[j]
        wds, cg = cgs[j]
        need = list(dict.fromkeys(c[0] for c in cells))
        if len(need) > BUF_SLOTS:
            sys.exit(f'frame {i}: {len(need)} distinct tiles, a buffer holds {BUF_SLOTS}')
        slot = {d: held[buf][d] for d in need if d in held[buf]}    # still there from two frames ago
        used = set(slot.values())
        free = (s for s in range(BUF_SLOTS) if s not in used)
        base = 0 if buf == 0 else CEL_BASE
        up = []
        for d in need:
            if d not in slot:
                slot[d] = next(free)
                up += cmd_vram((base + slot[d]) * 16, d)
        ent = [slot[c[0]] | c[1] << 10 for c in cells]
        body = merge_runs(up) + cmd_vram(BUF_MAP[buf] + ROW0 * 32, words(ent))
        switch = cmd_cgram(nb * 16, cg) + cmd_reg(0x2C, BUF_TM[buf])   # its palettes and layer, together
        fi = max(fields[i], last + 1)
        if i == a and cont:                                         # no cut: the background palettes with the switch
            switch = cmd_cgram(0, cg0 + cg) + cmd_reg(0x2C, BUF_TM[buf])
            caps = [(f, PACKET) for f in range(last + 1, fi + 1)]
        elif i == a:                                                # the cut: registers, empty rows
            body = cmd_reg(0x08, BUF_MAP[0] >> 8) + cmd_scroll(0, 4) + edges_buffers() + body
            if cg0:
                body = cmd_cgram(0, cg0) + body
            if grad:
                body = cmd_gradient(sc.grad) + body
            elif grad_on:
                body = cmd_gradient(None) + body
            k = 0
            while k * PACKET_BLANK + PACKET < size(body) + size(switch) + 4096:
                k += 1
            fi = max(fi, last + k + 1)
            for f in range(fi - k, fi):
                blank[f] = True
            caps = [(f, PACKET_BLANK) for f in range(fi - k, fi)] + [(fi, PACKET)]
        else:
            caps = [(f, PACKET) for f in range(last + 1, fi + 1)]
        while True:
            while len(packets) <= caps[-1][0] + 1:
                packets.append([])
                blank.append(False)
            if sum(r - size(packets[f]) for f, r in caps) >= (size(body) + size(switch)) * 1.06 + 512:
                break
            caps.append((caps[-1][0] + 1, PACKET))                  # too much for its fields: it shows late
        fi = caps[-1][0]
        if i not in (a, b - 1) and fi - fields[i] > MAX_LAG:       # too far behind the sound: drop this frame
            shown_at[i] = last
            dropped += 1
            continue
        held[buf] = slot
        shows += 1
        lates += fi > fields[i]
        fill(packets, [(f, r - (size(switch) if f == fi else 0)) for f, r in caps], body)
        packets[fi] += switch
        last = shown_at[i] = fi
        img = untile(np.array([wds[c[1]][np.maximum(c[2].astype(int) - 1, 0)] for c in cells]))
        if grad:
            img = np.where(untile(np.array([c[2] for c in cells])) > 0, img, sc.grad[:, None])
        shown[fi] = (i, window(img))
    if dropped:
        print(f'  {dropped} frames dropped to stay within {MAX_LAG} fields of the sound')
    STATS['dropped'] += dropped
    STATS['buffered'] += 1
    return last, lates, dict(held=held, shows=shows)


STATS = {}                          # the last encode_unit's numbers (bulk.py's report)


def field_frame(sc, bg, cel, track, i, j, fields, last, packets, blank, shown):
    """Frame i of a scene whose background scrolls every field: each field from the last switch to this frame's
    gets the background tiles its camera newly shows and its scroll, in its own packet; the frame's cels fill the
    room left (a frame too heavy shows up to LATE fields late; the background keeps its own time).  Returns (the
    switch field, (the cel ring, late?))."""
    keys = sc.cel_keys[j]
    cells = [None if k is None else (sc.cel_of[k][1], sc.cel_of[k][0]) for k in keys]
    fi = max(fields[i], last + 1)
    for late in range(LATE + 1):
        sw = fi + late
        while len(packets) <= sw + LATE + 1:
            packets.append([])
            blank.append(False)
        tbg, per, room = copy.deepcopy(bg), {}, []
        for f in range(last + 1, sw + 1):
            cam = track.get(f, track[max(track)])
            c = sum(tbg.frame(sc, view_cells(*cam), i), []) + cmd_scroll((cam[0] - PX0) % 512, (cam[1] - 4) % 1024)
            per[f] = (cam, c)
            room.append((f, PACKET - size(packets[f]) - size(c)))
        if min(r for _, r in room) < 0:
            if late == LATE:
                raise NoFit(f'frame {i}: the background alone does not fit its fields')
            continue
        trial = copy.deepcopy(cel)
        try:
            out = trial.transition(cells, room, False, i)
            break
        except NoFit:
            if late == LATE:
                raise
    bg.__dict__.update(tbg.__dict__)
    banded = trial.rolled > cel.rolled                              # (switched over several fields: no check between)
    for f, (cam, c) in per.items():
        packets[f] += c
        if f == sw:
            shown[f] = (i, sc.picture(j, cam))
        elif not banded:
            shown[f] = (i - 1, sc.picture(j - 1, cam))
    for f, c in out.items():
        packets[f] += c
    return sw, (trial, late > 0)


def encode_unit(clip, path):
    """One path of a clip as a stream: (bytes, ending in the end marker; field -> (frame, picture) for the check;
    the field of each frame's packet)."""
    STATS.clear()
    STATS.update(dropped=0, buffered=0, steps_moved=0)
    frames = load_frames(clip, path)
    n = len(frames)
    starts = [i for i, f in enumerate(frames) if f[1]] + [n]
    if starts[0]:
        starts.insert(0, 0)
    fields = [round(f * FIELD_HZ / FPS) + 2 for f in range(n)]      # the field each frame's packet is in
    packets = [[] for _ in range(fields[-1] + 2)]
    blank = [False] * len(packets)
    shown = {}                                                      # field -> (frame, picture) for the check
    shown_at, last, lates = [0] * n, -1, 0                          # the field each frame switches in
    cel, restore = CelRing(), False
    grad_on = False                                                 # a gradient scene left HDMA on (the player turns
    whole = None                                                    # (WHOLE units: the buffers' state)
    for s, (a, b) in enumerate(zip(starts, starts[1:])):              # it off when a unit starts)
        sc = Scene(clip, frames[a:b])
        print(f'scene {s}: frames {a}-{b - 1}, {sc.ncol} columns ({sc.n_bg} background tiles shown), '
              f'{sc.n_cel} cel tiles, camera x {min(sc.sx)}..{max(sc.sx)} y {min(sc.sy)}..{max(sc.sy)}')
        if clip in WHOLE:                                           # one layer, one shot: every scene drawn whole
            last, n_late, whole = buffered_scene(sc, a, b, fields, packets, blank, shown, shown_at, last, grad_on,
                                                 cont=whole if s else None, whole=True)
            grad_on, lates = False, lates + n_late
            cel, restore = CelRing(), True
            continue
        if sc.needs_buffers():
            last, n_late, _ = buffered_scene(sc, a, b, fields, packets, blank, shown, shown_at, last, grad_on)
            grad_on = sc.grad is not None
            lates += n_late
            cel, restore = CelRing(), True                          # (the buffers overwrote the cel slots, maps)
            continue
        snap = (copy.deepcopy(packets), blank[:], dict(shown), shown_at[:], last, lates, copy.deepcopy(cel), restore,
                grad_on)
        track = sc.field_track(fields[a:b])                     # a moving camera: the background every field
        while True:
            try:
                cg = b''.join(struct.pack('<H', 0) + words(sc.words[p]) for p in range(8))
                bg = BgRing()                                               # (the cut frees everything)
                if track:
                    print(f'  background scrolled every field ({len(set(track.values()))} positions)')
                for i in range(a, b):
                    j = i - a
                    if track and i > a:
                        last, lt = field_frame(sc, bg, cel, track, i, j, fields, last, packets, blank, shown)
                        cel = lt[0]
                        lates += lt[1]
                        shown_at[i] = last
                        continue
                    cam0 = track[fields[a]] if track else (sc.sx[j], sc.sy[j])
                    bg_cmds, at_switch = ([], []) if sc.grad is not None else bg.frame(sc, view_cells(*cam0), i)
                    keys = sc.cel_keys[j]
                    cells = [None if k is None else (sc.cel_of[k][1], sc.cel_of[k][0]) for k in keys]
                    scroll = at_switch + cmd_scroll((cam0[0] - PX0) % 512, (cam0[1] - 4) % 1024)   # (with the switch)
                    body = bg_cmds
                    fi = max(fields[i], last + 1)                           # (after a late frame the next may be too)
                    if i == a:                                              # scene cut: blank fields before the frame
                        if sc.grad is not None:                             # the gradient: BG1 (cels) alone over it
                            body = cmd_reg(0x08, 0x75) + cmd_reg(0x2C, 0x01) + cmd_cgram(0, cg) + cmd_gradient(sc.grad) + body
                        else:
                            body = cmd_reg(0x08, 0x75) + cmd_reg(0x2C, 0x03) + cmd_cgram(0, cg) + body   # the layered mode
                            if grad_on:
                                body = cmd_gradient(None) + body
                        grad_on = sc.grad is not None
                        if s == 0 or restore:                               # the empty tiles, and the map rows around the
                            body = edges() + body                           # picture (any unit can come first)
                            restore = False
                        est = size(body) + (32 + 4) * len({c[0] for c in cells if c is not None}) + 4096
                        k = 0
                        while k * PACKET_BLANK + PACKET < est:
                            k += 1
                        fi = max(fi, last + k + 1)
                        for f in range(fi - k, fi):
                            blank[f] = True
                        caps = [(f, PACKET_BLANK) for f in range(fi - k, fi)] + [(fi, PACKET)]
                    else:                                                   # spread over the fields since the last frame
                        caps = [(f, PACKET) for f in range(last + 1, fi + 1)]
                    while len(packets) <= fi + LATE + 1:
                        packets.append([])
                        blank.append(False)
                    fill(packets, [(f, r - (size(scroll) if f == fi else 0)) for f, r in caps], body)
                    for late in range(LATE + 1):                            # a frame too heavy for its fields shows late
                        here = caps + [(fi + d, PACKET) for d in range(1, late + 1)]
                        sw = here[-1][0]
                        room = [(f, r - size(packets[f]) - (size(scroll) if f == sw else 0)) for f, r in here]
                        trial = copy.deepcopy(cel)
                        try:
                            out = trial.transition(cells, room, i == a, i)
                            break
                        except NoFit:
                            if late == LATE:
                                raise
                    cel, fi = trial, sw
                    lates += late > 0
                    for f, c in out.items():
                        packets[f] += c
                    packets[fi] += scroll
                    last = shown_at[i] = fi
                    shown[fi] = (i, sc.picture(j, cam0 if track else None))
                break
            except NoFit as e:
                packets[:] = copy.deepcopy(snap[0])            # (a copy: a retry must not write into the snapshot)
                blank[:], shown_at[:] = snap[1], snap[3]
                shown.clear()
                shown.update(snap[2])
                last, lates, grad_on = snap[4], snap[5], snap[8]
                if track:                                       # first: the camera per frame (as the 3DO)
                    print(f'  {e}: background per frame instead')
                    track, cel, restore = None, copy.deepcopy(snap[6]), snap[7]
                    continue
                print(f'  {e}: drawn whole instead')            # too heavy to show layered: whole frames
                last, n_late, _ = buffered_scene(sc, a, b, fields, packets, blank, shown, shown_at, last, grad_on)
                grad_on = sc.grad is not None
                lates += n_late
                cel, restore = CelRing(), True
                break
    data = bytearray()
    for k, p in enumerate(packets):
        assert len(p) < 256
        data += bytes([1 if blank[k] else 0, len(p)]) + b''.join(p)
    data += bytes([0x80, 0])
    big = max(size(p) for k, p in enumerate(packets) if not blank[k])
    most = max(len(p) for k, p in enumerate(packets) if not blank[k])
    print(f'{clip}_{path}: {len(data) / 1024:.0f} KB, {len(packets)} fields ({len(packets) / FIELD_HZ:.1f} s), '
          f'{sum(blank)} blank fields, {cel.rolled} frames switched in bands, {lates} late, largest normal packet {big} (cost), '
          f'at most {most} commands, '
          f'{len(data) / (len(packets) / FIELD_HZ) / 1024:.0f} KB/s')
    STATS.update(kb=round(len(data) / 1024), fields=len(packets), blank=sum(blank), banded=cel.rolled, late=lates,
                 frames=n, scenes=len(starts) - 1, biggest=big, commands=most,
                 kbps=round(len(data) / (len(packets) / FIELD_HZ) / 1024))
    return bytes(data), shown, shown_at


INPUT_BIT = {'up': 0, 'down': 1, 'left': 2, 'right': 3, 'fire': 4}    # the player's move bits; outcome = bit + 1


def segment(clip, name='bd13msu', script=''):
    """A clip with its deaths: the main path, each death path, the revive clip.  Writes <name>.msu (the units'
    streams), <name>-<k+1>.pcm (unit k's audio) and units.inc (the unit table and decisions for the player).
    A death decision gives each input (none, up, down, left, right, fire) a unit: the death scene, or $FF for
    the right move.  After a death scene comes the revive clip, after the revive clip the clip again; at its end
    the clip starts over (a test loop: the clip's JUMP is not followed).
    script: test presses for the player ('' none; 'good' every right move; 'die:N' miss decision N once)."""
    logic = json.load(open(os.path.join(SRC3, 'logic_ALL.json')))
    L = logic[clip]
    units = [(clip, 'main')] + [(clip, p) for p in L['paths'] if p != 'main']
    if L.get('revive') and L['revive'] in logic:
        units.append((L['revive'], 'main'))
    index = {f'{c}_{p}': k for k, (c, p) in enumerate(units)}
    revive = index.get(f"{L.get('revive')}_main")
    msu, table, checks = bytearray(), [], []
    for k, (c, p) in enumerate(units):
        cache = os.path.join(BUILD, 'units', f'{c}_{p}.pkl')
        if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(__file__):
            data, shown, fields = pickle.load(open(cache, 'rb'))
        else:
            data, shown, fields = encode_unit(c, p)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            pickle.dump((data, shown, fields), open(cache, 'wb'))
        checks.append((f'{c}_{p}', data, shown))
        msu += bytes(-len(msu) % 2048)                              # each unit starts on a 2 KB boundary
        off = len(msu)
        msu += data
        write_pcm(c, p, len(fields), os.path.join(BUILD, f'{name}-{k + 1}.pcm'))
        decisions = []
        if (c, p) == (clip, 'main'):
            for d in L['decisions']:
                if d['path'] != 'main' or d['kind'] != 'death':
                    continue
                f0, f1 = d['frame'], min(d['frame'] + d['window'], len(fields) - 1)
                out = [0xFF if d['outcomes'][i] is None else index[f'{clip}_{d["outcomes"][i]}']
                       for i in ('none', 'up', 'down', 'left', 'right', 'fire')]
                decisions.append((fields[f0] + 1, fields[f1] + 1, out))   # from the field its first frame shows
            nxt = 0
        elif c == clip:
            nxt = revive if revive is not None else 0               # a death scene: the revive clip
        else:
            nxt = 0                                                 # the revive clip: the clip again
        table.append((off, k + 1, nxt, decisions))
    open(os.path.join(BUILD, name + '.msu'), 'wb').write(msu)
    write_units(table, units, script)
    print(f'{name}.msu: {len(units)} units, {len(msu) / 2 ** 20:.1f} MB')
    return checks


def write_units(table, units, script):
    """units.inc: the unit table, the decisions and the test script, for player.asm (same format as pack_game.py,
    which builds the whole game; this one clip loops, so nothing here uses routes, COND rules or restarts)."""
    out = [f'; made by encode_snes.py: {", ".join(f"{k} {c}_{p}" for k, (c, p) in enumerate(units))}',
           '.DEFINE START_UNIT 0', '.DEFINE SKIP_UNIT 0', 'TitleButtons:', 'UnitTable:']
    for k, (off, track, nxt, decs) in enumerate(table):
        out.append(f"  .dw ${off & 0xFFFF:04X}, ${off >> 16:04X}, {f'Dec{k}' if decs else 0}, {track}, {nxt}, $FFFF, 0"
                   f'\n  .db {len(decs)}, 0   ; {units[k][0]}_{units[k][1]}')
    for k, (off, track, nxt, decs) in enumerate(table):
        if decs:
            out.append(f'Dec{k}:')
        for ws, we, o in decs:
            out.append(f'  .dw {ws}, {we}, $FFFF, 0, ' + ', '.join(f'${0xFFFF if v == 0xFF else v:04X}' for v in o))
    out.append('Script:')                                           # unit, field, move bits; used in order
    main = table[0][3]
    passes = []
    if script == 'good':
        passes = [[True] * len(main)]
    elif script.startswith('die:'):
        n = int(script[4:])
        passes = [[i < n for i in range(n + 1)], [True] * len(main)]
    for ok in passes:
        for (ws, we, o), good in zip(main, ok):
            right = [i - 1 for i in range(1, 6) if o[i] == 0xFF]
            if good and right:                                      # (no record: no press, right or wrong)
                out.append(f'  .dw 0, {ws + 6}\n  .db {1 << right[0]}')
    out.append('  .dw $FFFF')
    open(os.path.join(HERE, 'units.inc'), 'w').write('\n'.join(out) + '\n')


def write_pcm(clip, path, nframes, out):
    d, _ = clip_dir(clip)
    w = wave.open(os.path.join(d, f'audio_{path}.wav'))
    x = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float64).reshape(-1, w.getnchannels())
    n_out = int(round(len(x) * 44100 / w.getframerate()))
    y = np.stack([np.fft.irfft(np.fft.rfft(x[:, c % x.shape[1]]), n_out) * n_out / len(x) for c in range(2)], 1)
    import match_cdi_audio
    pcm = match_cdi_audio.cdi_pcm(f'{clip}_{path}', n_out, y)  # the CD-i's stereo, where it has this path
    if pcm is None:
        pcm = np.clip(np.rint(y), -32768, 32767)
    pcm = pcm.astype('<i2')
    open(out, 'wb').write(b'MSU1' + struct.pack('<I', 0) + pcm.tobytes())


# ---- check: play the stream like the PPU would ---------------------------------------------------------------
GRAB_FIELDS = False                 # check_any_order.py: simulate's grabs also name their field


def simulate(data, shown, keep=True, dirty=False):
    """Play a stream like the PPU: VRAM, CGRAM and the registers the stream sets (BG2's map, the main screen
    layers, BG2's scroll); compare each frame's picture with what the encoder meant.
    dirty: start from junk, as if any other unit had just played (check_any_order.py)."""
    vram, cgram = bytearray(65536), bytearray(512)
    reg = {0x08: 0x75, 0x2C: 0x03}                                  # as the player sets them up
    p, field, bad, hofs, vofs = 0, 0, 0, 0, 0x3FC
    if dirty:
        vram, cgram = bytearray(os.urandom(65536)), bytearray(os.urandom(512))
        reg = {0x08: 0x3C, 0x2C: 0x01}                              # (as the alternating buffers leave them)
        hofs, vofs = 123, 77
    grabs, grad = [], None                                          # grad: the fixed colour per screen line (HDMA)
    while True:
        flags, n = data[p], data[p + 1]
        p += 2
        if flags & 0x80:
            break
        for _ in range(n):
            t = data[p]
            if t in (0, 2):
                a, k = data[p + 1] | data[p + 2] << 8, data[p + 3] | data[p + 4] << 8
                if t == 0:
                    vram[a * 2:a * 2 + k] = data[p + 5:p + 5 + k]
                else:
                    for q in range(k // 2):
                        w = (a + 32 * q) & 0x7FFF
                        vram[w * 2:w * 2 + 2] = data[p + 5 + 2 * q:p + 7 + 2 * q]
                p += 5 + k
            elif t == 1:
                a, k = data[p + 1], data[p + 2] | data[p + 3] << 8
                cgram[a * 2:a * 2 + k] = data[p + 4:p + 4 + k]
                p += 4 + k
            elif t == 3:
                hofs, vofs = data[p + 1] | data[p + 2] << 8, data[p + 3] | data[p + 4] << 8
                p += 5
            elif t == 5:
                la, lb = data[p + 1] | data[p + 2] << 8, data[p + 3] | data[p + 4] << 8
                ta, tb = data[p + 5:p + 5 + la], data[p + 5 + la:p + 5 + la + lb]
                p += 5 + la + lb
                grad = None if not la else hdma_lines(ta, 2, 0) | hdma_lines(tb, 1, 0)
            else:
                reg[data[p + 1]] = data[p + 2]
                p += 3
        if field in shown and not flags & 1:
            i, want = shown[field]
            got = render(vram, cgram, hofs, vofs, reg, grad)
            if not np.array_equal(got, want):
                bad += 1
            grabs.append((field, i, got) if GRAB_FIELDS else (i, got) if keep else i)
        field += 1
    return bad, grabs


def decode_tile(vram, t):
    raw = np.frombuffer(bytes(vram[t * 32:t * 32 + 32]), np.uint8)
    lo, hi = raw[:16].reshape(8, 2), raw[16:].reshape(8, 2)
    planes = [lo[:, 0], lo[:, 1], hi[:, 0], hi[:, 1]]
    return sum(np.unpackbits(planes[b][:, None], axis=1).astype(np.int64) << b for b in range(4))


def hdma_lines(table, width, _):
    """An HDMA table to $2132 (COLDATA) -> the fixed colour's bits it sets per screen line (224, BGR555 words):
    each write sets the channels its bits 5-7 name to its bits 0-4."""
    out, line, k = np.zeros(224, np.int64), 0, 0
    while table[k]:
        n = table[k]
        w = 0
        for b in table[k + 1:k + 1 + width]:
            v = b & 31
            w |= (v if b & 0x20 else 0) | (v << 5 if b & 0x40 else 0) | (v << 10 if b & 0x80 else 0)
        out[line:line + n] = w
        line += n
        k += 1 + width
    return out


def render(vram, cgram, hofs, vofs, reg, grad=None):
    cg = np.frombuffer(bytes(cgram), '<u2').astype(np.int64)
    out = np.full((H, W), cg[0])                                    # the backdrop
    if grad is not None:                                            # + the fixed colour (colour math on the backdrop)
        f = grad[LINE_SHOW:LINE_SHOW + H, None]
        add = lambda sh: np.minimum(((out >> sh) & 31) + ((f >> sh) & 31), 31) << sh
        out = add(0) | add(5) | add(10)
    tm = reg[0x2C]
    if tm & 2:                                                      # BG2: tiles from 0, map from BG2SC
        base, wide = (reg[0x08] & 0xFC) << 8, reg[0x08] & 1
        m2 = np.frombuffer(bytes(vram[base * 2:base * 2 + (4096 if wide else 2048)]), '<u2').reshape(-1, 32, 32)
        m2 = np.concatenate(list(m2), 1)                            # 32 rows x 32 or 64 columns
        bg = np.zeros((256, m2.shape[1] * 8), np.int64)
        for ty in range(32):
            for tx in range(m2.shape[1]):
                e = int(m2[ty, tx])
                px = decode_tile(vram, e & 1023)
                bg[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = np.where(px > 0, cg[(e >> 10 & 7) * 16 + px], -1)
        lines = (np.arange(H) + 4 + vofs) % 256                     # screen line 4 + j shows BG2 line 4 + j + vofs
        v = bg[lines][:, (np.arange(W) + hofs) % bg.shape[1]]
        out = np.where(v >= 0, v, out)                              # (transparent: the backdrop)
    if tm & 1:                                                      # BG1 over it: tiles from CEL_BASE, map MAP1
        m1 = np.frombuffer(bytes(vram[MAP1 * 2:MAP1 * 2 + 2048]), '<u2').reshape(32, 32)
        for ty in range(TH):
            for tx in range(TW):
                e = int(m1[ROW0 + ty, tx])
                px = decode_tile(vram, CEL_BASE + (e & 1023))
                blk = out[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8]
                blk[px > 0] = cg[(e >> 10 & 7) * 16 + px[px > 0]]
    return window(out)


def main():
    """python encode_snes.py <clip> [script]: the clip, its death scenes and its revive clip (see segment)."""
    clip = sys.argv[1] if len(sys.argv) > 1 else 'BD03_003'
    script = sys.argv[2] if len(sys.argv) > 2 else ''
    os.makedirs(SHOTS, exist_ok=True)
    for name, data, shown in segment(clip, script=script):
        bad, grabs = simulate(data, shown)
        print(f'check {name}: {len(grabs)} frames shown, {bad} differ from the encoded pictures')
        pick = grabs[::max(1, len(grabs) // 12)][:12]
        sheet = Image.new('RGB', (W * 4, H * 3))
        for k, (i, img) in enumerate(pick):
            sheet.paste(Image.fromarray(rgb555(img).astype(np.uint8)), (W * (k % 4), H * (k // 4)))
        sheet.save(os.path.join(SHOTS, f'{name}_sim.png'))


if __name__ == '__main__':
    main()

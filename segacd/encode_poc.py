"""Offline encoder + preview for the Sega CD player (its first encoder; its helpers are still used).

Takes extracted 3DO frames (background + sprite cel) and DOS audio for one clip and writes a
tile-based stream (see STREAM FORMAT (version 2: all fields big-endian and word-aligned so the 68000 can read them directly)
  header  'BDSC', u8 version(2), u8 fps, u16 audio_rate, u16 frames, u8 tiles_w, u8 tiles_h, u16 scenes, u16 pad  (16 bytes)
  record  u16 type, u32 body length (even), body
  SCENE (type 1) body: 64 CRAM words, u16 priority bits (bit n = line n is NORMAL brightness), u16 ntiles (incl. blank
          tile 0), ntiles*32 tile bytes, tiles_w*tiles_h map words
          map word: bit15 priority, bits14-13 palette line, bit12 vflip, bit11 hflip, bits10-0 tile number
          relative to the scene's tile pool base (0 = blank)
  FRAME (type 2) body: 1200 audio bytes (RF5C164 sign-magnitude), u16 k (number of cel tiles), mask (tiles_w*tiles_h/8
          bytes, LSB first), cel line bits (1 per set mask bit, padded to an even number of bytes), k*32 tile bytes
  Cel tiles use palette line 2 or 3 and priority 1.
"""
import os, sys, struct, csv, wave
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC3 = os.path.join(HERE, '..', 'bd13', '3do', 'extracted')
SRCD = os.path.join(HERE, '..', 'bd13', 'dos', 'extracted')
WIN_W, WIN_H = 224, 192
TW, TH = WIN_W // 8, WIN_H // 8
NT = TW * TH
FPS, AUD_RATE = 12, 14400
DOS_AUD_PER_FRAME = 1332          # DOS stream: 2664 bytes of 8-bit stereo per frame = 1332 samples per channel
# Shadow/Highlight lines stay off: a high-priority cel tile cancels shadowing under its transparent pixels, which
# shows up as bright squares around the sprites.  Re-enable once the cel is drawn with hardware sprites.
USE_SHADOW = False
TONE = len(sys.argv) > 4 and sys.argv[4] == 'tone'      # 4th command-line argument "tone": replace the audio with a test tone
AUD_PER_FRAME = AUD_RATE // FPS

# ---- colour model -------------------------------------------------------------------------
# Genesis DAC levels (from memory): normal = every other step of the 15-step table, shadow = first 8 steps.
TABLE = np.array([0, 29, 52, 70, 87, 101, 116, 130, 144, 158, 172, 187, 206, 228, 255], float)
NORM, SHAD = TABLE[0::2], TABLE[0:8]


def _grid(levels):
    return np.array([[levels[r], levels[g], levels[b]] for r in range(8) for g in range(8) for b in range(8)])


GN, GS = _grid(NORM), _grid(SHAD)


def srgb2lin(a):
    a = a / 255.0
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def lin2srgb(a):
    a = np.clip(a, 0, 1)
    return np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055) * 255


def oklab(rgb):
    l = srgb2lin(np.asarray(rgb, float))
    M1 = np.array([[.4122214708, .5363325363, .0514459929], [.2119034982, .6806995451, .1073969566], [.0883024619, .2817188376, .6299787005]])
    M2 = np.array([[.2104542553, .7936177850, -.0040720468], [1.9779984951, -2.4285922050, .4505937099], [.0259040371, .7827717662, -.8086757660]])
    return np.cbrt(l @ M1.T) @ M2.T


LN, LS = oklab(GN), oklab(GS)


def kmeans(X, k, it=10, seed=0):
    rng = np.random.default_rng(seed)
    if len(X) <= k:
        return X.copy()
    C = X[rng.choice(len(X), k, replace=False)]
    for _ in range(it):
        a = ((X[:, None] - C[None]) ** 2).sum(2).argmin(1)
        for j in range(k):
            m = a == j
            if m.any():
                C[j] = X[m].mean(0)
    return C


def fit_line(X, L, k=15):
    """Palette of up to k grid-colour codes (0..511 into the 8x8x8 grid) for OKLab samples X."""
    if len(X) == 0:
        return np.array([0])
    if len(X) > 20000:
        X = X[np.random.default_rng(0).choice(len(X), 20000, replace=False)]
    C = kmeans(X, k)
    return np.unique(((C[:, None] - L[None]) ** 2).sum(2).argmin(1))


def line_error(X, L, codes):
    return ((X[:, None] - L[codes][None]) ** 2).sum(2).min(1).sum()


BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0 - 0.5


def tiles(a):
    h, w = a.shape[:2]
    return a.reshape(h // 8, 8, w // 8, 8, *a.shape[2:]).swapaxes(1, 2).reshape(-1, 64, *a.shape[2:])


DITHER = tiles(np.tile(BAYER, (WIN_H // 4, WIN_W // 4)))      # (NT, 64)


def map_tiles(T, mask, lines, grids, dither=0.045):
    """T (n,64,3) OKLab; mask (n,64) opaque. Returns best line per tile and palette entries (n,64) in 1..15."""
    n = len(T)
    err = np.full((n, len(lines)), np.inf)
    idx = np.zeros((len(lines), n, 64), np.uint8)
    Td = T + DITHER[:, :, None] * dither
    for li, (codes, L) in enumerate(zip(lines, grids)):
        P = L[codes]
        j = ((Td[:, :, None, :] - P[None, None]) ** 2).sum(3).argmin(2)
        idx[li] = j + 1
        err[:, li] = (((T - P[j]) ** 2).sum(2) * mask).sum(1)
    best = err.argmin(1)
    out = np.take_along_axis(idx, best[None, :, None].repeat(64, 2), 0)[0]
    return best, out


def pack_tile(px):
    return bytes((int(px[i]) << 4) | int(px[i + 1]) for i in range(0, 64, 2))


# ---- source loading ---------------------------------------------------------------------------
def scale_rgb(img):
    a = srgb2lin(np.array(img.convert('RGB'), float))
    ch = [np.array(Image.fromarray(a[:, :, c].astype(np.float32), 'F').resize((WIN_W, WIN_H), Image.BOX)) for c in range(3)]
    return np.clip(lin2srgb(np.dstack(ch)), 0, 255)


def scale_cel(img):
    a = np.array(img.convert('RGBA'), float)
    al = a[:, :, 3] / 255
    pm = srgb2lin(a[:, :, :3]) * al[:, :, None]
    ch = [np.array(Image.fromarray(pm[:, :, c].astype(np.float32), 'F').resize((WIN_W, WIN_H), Image.BOX)) for c in range(3)]
    A = np.array(Image.fromarray(al.astype(np.float32), 'F').resize((WIN_W, WIN_H), Image.BOX))
    rgb = lin2srgb(np.dstack(ch) / np.maximum(A, 1e-4)[:, :, None])
    return np.clip(rgb, 0, 255).astype(np.float32), A >= 0.5


def clip_disc(clip):
    """The 3DO disc a clip's step 1 export is in ('1', or '2' for the clips only on disc 2)."""
    return '1' if os.path.isdir(os.path.join(SRC3, 'disc1', clip)) else '2'


def clip_dir(clip):
    return os.path.join(SRC3, 'disc' + clip_disc(clip), clip)


def load_clip(clip, nframes):
    d = clip_dir(clip)
    rows = [r for r in csv.DictReader(open(os.path.join(SRC3, 'manifest.csv')))
            if r['disc'] == '1' and r['file'] == clip + '.DAT']
    bg = cel = None
    out = []
    for r in rows[:nframes]:
        f = int(r['frame'])
        ch = r['chunks'].split()
        new = False
        p = os.path.join(d, f'BACK_{f:05d}.png')
        if 'BACK' in ch and os.path.exists(p):
            bg = scale_rgb(Image.open(p))
            new = True
        p = os.path.join(d, f'CL16_{f:05d}.png')
        if 'CL16' in ch and os.path.exists(p):
            cel = scale_cel(Image.open(p))
        out.append((f, new, bg, cel))
    return out


def load_audio(dos_clip, nframes):
    if TONE:                                                            # calibration: 1 kHz sine at the 14.4 kHz ring rate
        t = np.arange(nframes * AUD_PER_FRAME)
        v = np.rint(100 * np.sin(2 * np.pi * 1000 * t / AUD_RATE)).astype(int)
        return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)
    w = wave.open(os.path.join(SRCD, f'{dos_clip:05d}', 'audio.wav'))
    raw = np.frombuffer(w.readframes(w.getnframes()), np.uint8).astype(float) - 128
    mono = raw.reshape(-1, 2).mean(1)                                   # 16 kHz stereo -> mono
    mono = mono[:nframes * DOS_AUD_PER_FRAME]                           # only the audio of the frames we encode
    n = nframes * AUD_PER_FRAME
    a = np.interp(np.linspace(0, len(mono) - 1, n), np.arange(len(mono)), mono)   # -> 14.4 kHz
    v = np.clip(np.rint(a), -126, 126).astype(int)
    # RF5C164 sign-magnitude (bit7 = sign, assumed 1 = positive); 0xFF is the loop marker, so magnitude <= 0x7E
    return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)


# ---- encoding -----------------------------------------------------------------------------------
def cram_word(code):
    r, g, b = (code >> 6) & 7, (code >> 3) & 7, code & 7
    return (b << 9) | (g << 5) | (r << 1)


def encode(clip, dos_clip, nframes, out_prefix):
    frames = load_clip(clip, nframes)
    audio = load_audio(dos_clip, len(frames))
    scenes = []
    cur = None
    for i, (f, new, bg, cel) in enumerate(frames):
        if new or cur is None:
            cur = {'start': i, 'bg': bg, 'cels': []}
            scenes.append(cur)
        cur['cels'].append((i, cel))
    stream = bytearray(b'BDSC' + struct.pack('>BBHHBBHH', 2, FPS, AUD_RATE, len(frames), TW, TH, len(scenes), 0))
    stats = {'scene': [], 'frame': [], 'tiles': []}
    for s in scenes:
        # --- background: palette lines 0,1 (shadow lines allowed)
        lab = oklab(s['bg'])
        T = tiles(lab)
        feat = np.concatenate([T.mean(1), T.std(1)], 1)
        cent = kmeans(feat, 2)
        assign = ((feat[:, None] - cent[None]) ** 2).sum(2).argmin(1)
        bl, bgrid, bshadow = [None] * 2, [None] * 2, [0] * 2
        for _ in range(4):
            for l in range(2):
                X = T[assign == l].reshape(-1, 3) if (assign == l).any() else lab.reshape(-1, 3)[::40]
                cands = []
                for g, L in (((0, LN), (1, LS)) if USE_SHADOW else ((0, LN),)):
                    c = fit_line(X, L)
                    cands.append((line_error(X, L, c), g, c))
                e, g, c = min(cands, key=lambda t: t[0])
                bl[l], bshadow[l], bgrid[l] = c, g, (LN, LS)[g]
            errs = np.stack([((T[:, :, None] - bgrid[l][bl[l]][None, None]) ** 2).sum(3).min(2).sum(1) for l in range(2)], 1)
            assign = errs.argmin(1)
        best, idx = map_tiles(T, np.ones((NT, 64), bool), bl, bgrid)
        pool, seen, mapw = [], {}, []
        for t in range(NT):
            px = idx[t].reshape(8, 8)
            hit = None
            for v, fl in ((px, 0), (px[:, ::-1], 0x800), (px[::-1], 0x1000), (px[::-1, ::-1], 0x1800)):
                k = (int(best[t]), v.tobytes())
                if k in seen:
                    hit = (seen[k], fl)
                    break
            if hit is None:
                seen[(int(best[t]), px.tobytes())] = len(pool)
                pool.append(pack_tile(px.reshape(-1)))
                hit = (len(pool) - 1, 0)
            prio = 0 if bshadow[best[t]] else 1
            mapw.append((prio << 15) | (int(best[t]) << 13) | hit[1] | (1 + hit[0]))
        # --- cel palette lines 2,3 (normal brightness) from sampled cel pixels of the scene
        parts = [oklab(c[0])[c[1]] for _, c in s['cels'][::4] if c is not None]
        X = np.concatenate(parts) if parts else np.zeros((1, 3))
        cent = kmeans(X, 2, seed=1)
        a2 = ((X[:, None] - cent[None]) ** 2).sum(2).argmin(1)
        cl = [fit_line(X[a2 == l] if (a2 == l).any() else X, LN) for l in range(2)]
        pal = [0] * 64
        for l in range(2):
            for j, c in enumerate(bl[l]):
                pal[l * 16 + 1 + j] = cram_word(int(c))
            for j, c in enumerate(cl[l]):
                pal[(2 + l) * 16 + 1 + j] = cram_word(int(c))
        pri_bits = [0 if bshadow[0] else 1, 0 if bshadow[1] else 1, 1, 1]
        rec = bytearray(b''.join(struct.pack('>H', w) for w in pal))
        rec += struct.pack('>HH', sum(b << i for i, b in enumerate(pri_bits)), len(pool) + 1)
        rec += bytes(32) + b''.join(pool) + b''.join(struct.pack('>H', w) for w in mapw)
        stream += struct.pack('>HI', 1, len(rec)) + rec
        s['rec'] = bytes(rec)
        stats['scene'].append((s['start'], len(rec), len(pool) + 1, bshadow))
        # --- frames
        for i, cel in s['cels']:
            if cel is None:
                mask = np.zeros(NT, bool); idxs = np.zeros((0, 64), np.uint8); lines = []
            else:
                rgb, op = cel
                Tc = tiles(oklab(rgb)); tm = tiles(op[:, :, None])[:, :, 0]; mask = tm.any(1)
                best_c, idx_c = map_tiles(Tc, tm, cl, [LN, LN])
                idxs = np.where(tm, idx_c, 0)[mask]; lines = [int(x) for x in best_c[mask]]
            lbits = bytearray(((len(lines) + 15) // 16) * 2)
            for j, l in enumerate(lines):
                lbits[j // 8] |= l << (j % 8)
            fr = bytearray(audio[i * AUD_PER_FRAME:(i + 1) * AUD_PER_FRAME]) + struct.pack('>H', len(lines)) \
                + np.packbits(mask, bitorder='little').tobytes() + bytes(lbits) + b''.join(pack_tile(t) for t in idxs)
            stream += struct.pack('>HI', 2, len(fr)) + fr
            stats['frame'].append(len(fr)); stats['tiles'].append(int(mask.sum()))
    with open(out_prefix + '.bin', 'wb') as f:
        f.write(stream)
    return bytes(stream), scenes, stats, frames


# ---- preview decoder: parses the binary stream and simulates the video chip -----------------------------
def parse(stream):
    assert stream[:4] == b'BDSC'
    ver, fps, ar, nfr, tw, th, nsc, _ = struct.unpack('>BBHHBBHH', stream[4:16])
    assert ver == 2
    p = 16
    scenes = []
    while p < len(stream):
        t, ln = struct.unpack('>HI', stream[p:p + 6]); body = stream[p + 6:p + 6 + ln]; p += 6 + ln
        if t == 1:
            pal = struct.unpack('>64H', body[:128]); pri, nt = struct.unpack('>HH', body[128:132])
            tl = [np.frombuffer(body[132 + 32 * k:164 + 32 * k], np.uint8) for k in range(nt)]
            mp = struct.unpack('>%dH' % (tw * th), body[132 + 32 * nt:132 + 32 * nt + 2 * tw * th])
            scenes.append({'pal': pal, 'pri': pri, 'tiles': [np.stack([a >> 4, a & 15], 1).reshape(8, 8) for a in tl], 'map': mp, 'frames': []})
        else:
            aud = body[:AUD_PER_FRAME]; mb = tw * th // 8
            kk = struct.unpack('>H', body[AUD_PER_FRAME:AUD_PER_FRAME + 2])[0]; o = AUD_PER_FRAME + 2
            mask = np.unpackbits(np.frombuffer(body[o:o + mb], np.uint8), bitorder='little').astype(bool)
            k = int(mask.sum()); assert k == kk; nb = ((k + 15) // 16) * 2
            lb = np.unpackbits(np.frombuffer(body[o + mb:o + mb + nb], np.uint8), bitorder='little')[:k]
            off = o + mb + nb
            tl = [np.frombuffer(body[off + 32 * j:off + 32 * j + 32], np.uint8) for j in range(k)]
            scenes[-1]['frames'].append((mask, lb, [np.stack([a >> 4, a & 15], 1).reshape(8, 8) for a in tl], aud))
    return (fps, ar, nfr, tw, th), scenes


def render(scene, frame, tw=TW, th=TH):
    pal = scene['pal']
    def lut(line, grid):
        out = np.zeros((16, 3))
        for e in range(1, 16):
            w = pal[line * 16 + e]; out[e] = (grid[(w >> 1) & 7], grid[(w >> 5) & 7], grid[(w >> 9) & 7])
        return out
    img = np.zeros((th * 8, tw * 8, 3))
    for t, w in enumerate(scene['map']):
        line = (w >> 13) & 3; px = scene['tiles'][w & 0x7FF]
        if w & 0x800: px = px[:, ::-1]
        if w & 0x1000: px = px[::-1]
        ty, tx = divmod(t, tw)
        img[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8] = lut(line, NORM if (w >> 15) else SHAD)[px]
    mask, lb, tl, _ = frame
    k = 0
    for t in np.where(mask)[0]:
        px = tl[k]; line = 2 + int(lb[k]); k += 1
        ty, tx = divmod(int(t), tw); blk = lut(line, NORM)[px]
        reg = img[ty * 8:ty * 8 + 8, tx * 8:tx * 8 + 8]
        reg[px > 0] = blk[px > 0]
    return np.clip(img, 0, 255).astype(np.uint8)


def delta_e(a, b):
    return (np.sqrt(((oklab(a) - oklab(b)) ** 2).sum(2)) * 100).mean()


if __name__ == '__main__':
    clip, dos_clip, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    out = os.path.join(HERE, 'out'); os.makedirs(out, exist_ok=True)
    stream, scenes, stats, frames = encode(clip, dos_clip, n, os.path.join(out, clip + ('_tone' if TONE else '')))
    for si, sc_ in enumerate(scenes):
        open(os.path.join(out, f'{clip}_scene{si}.bin'), 'wb').write(sc_['rec'])
    fb = np.array(stats['frame'])
    print(f'{clip}: {len(frames)} frames, {len(scenes)} scenes, stream {len(stream) / 1024:.0f} KB')
    print('scene records (start frame, bytes, tiles, shadow lines):', stats['scene'])
    print(f'frame bytes: mean {fb.mean():.0f}  p90 {np.percentile(fb, 90):.0f}  max {fb.max()}  (budget ~11,000 at 12 fps)')
    print(f'cel tiles per frame: mean {np.mean(stats["tiles"]):.0f}  max {max(stats["tiles"])}  (pool 512)')
    dur = len(frames) / FPS
    print(f'average stream rate {len(stream) / dur / 1000:.0f} KB/s over {dur:.1f} s')
    # --- preview: decode the .bin itself and compare to the scaled source
    hdr, sc = parse(stream)
    errs = []; shots = []
    fi = 0
    for si, s in enumerate(sc):
        for k, fr in enumerate(s['frames']):
            if fi % 6 == 0:
                img = render(s, fr)
                f, new, bg, cel = frames[fi]
                ref = bg.copy()
                if cel is not None:
                    ref[cel[1]] = cel[0][cel[1]]
                errs.append(delta_e(ref, img))
                if fi in (0, 10, 30, 60, 100, 130, 140) or (len(shots) < 8 and fi % 18 == 0):
                    shots.append((fi, ref.astype(np.uint8), img))
            fi += 1
    print(f'preview delta-E vs scaled source (sampled frames): mean {np.mean(errs):.2f}  max {np.max(errs):.2f}')
    shots = shots[:6]
    c = Image.new('RGB', (WIN_W * 2, WIN_H * len(shots)))
    for r, (fi, ref, img) in enumerate(shots):
        c.paste(Image.fromarray(ref), (0, r * WIN_H)); c.paste(Image.fromarray(img), (WIN_W, r * WIN_H))
    c.save(os.path.join(out, clip + '_preview.png'))
    print('preview frames (source | encoded):', [s[0] for s in shots])

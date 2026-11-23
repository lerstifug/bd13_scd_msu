"""Step 2 of 3 - Sega CD quality bitmaps from the 3DO export:
  python scd_bitmaps.py BD04 [unit names...] [--force] [--workers N]   -> scd/BD04/<unit>/ (see scd_format.py)
  python scd_bitmaps.py MENUS                                         -> scd/MENUS/ (pause overlay, save menu)
  python scd_bitmaps.py CHEAT [--force]                               -> scd/MENUS/CHEATMENU/ (the cheat menu)

Reads step 1's files only (bd13/3do/extracted: frame PNGs, logic_<stage>.json, audio_<path>.wav, camera.json).
Every colour decision happens here: scaling to the 224x192 window, the scene's four 15-colour lines on the
512-colour grid, normal/shadowed per cell, which cells get a second layer, pattern dithering, and which frame
cells change (encode_v3.py / encode_scroll.py / colour.py).  The result is written as indexed bitmaps of exactly
what the Sega CD will show - one background per scene and one cel layer per frame (the pixels that differ from
the background) - so they can be checked and touched up in a paint program before step 3
(scd_pack.py) turns them into the player's binaries.  The soundtrack becomes the 14.4 kHz 8-bit WAV the player
plays.  A unit whose folder already has unit.json is skipped (edits are kept) unless --force.
"""
import json, os, sys, time, wave
from multiprocessing import Pool
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'bd13', '3do', 'extracted')
OUT = os.path.join(HERE, 'scd')


def untile(t, tw):
    """(n,64) cells, tw per row -> (rows*8, tw*8)."""
    th = len(t) // tw
    return t.reshape(th, tw, 8, 8).swapaxes(1, 2).reshape(th * 8, tw * 8)


WHOLE = {'BD51_001'}    # clips drawn as one layer: every frame whole, smaller, one still scene per shot (the Dragon's
                        # Lair II trailer: its scrolling scenes one after another were too heavy for the drive)
# their picture (cell-aligned), centred in the window on black (the border never changes), and their frame pool:
# 160x136 with 360 merges no frame and does not stall above 135 KB/s (stall_sim with the ring; the drive: 153.6)
WHOLE_SIZE = tuple(int(v) for v in os.environ.get('BD13_WHOLE', '160x136').split('x'))
WHOLE_POOL = int(os.environ.get('BD13_WHOLE_POOL', 360))
PAN_POOL = 300          # pans change every cell: a smaller frame pool keeps them under the CD rate (1x, 150 KB/s)


def shown_colours(codes, idx):
    """OKLab colour of bitmap indices idx (n,64) with a scene's four lines of grid codes."""
    import scd_format as F, scd_pack as SP
    return SP.Pal(F.palette(codes)).lab[idx]


def seamless(T, cel, shown):
    """The target of a frame's changed cells: the cel's pixels as they are, and the rest as the background is
    already shown (not the 3DO colour).  A cell the cel touches then keeps its background pixels unless that
    costs nothing: re-quantising them on another pair of lines showed as blocks round the cels."""
    T = T.copy()
    T[~cel] = shown[~cel]
    return T


def static_bitmaps(bg, cels, seed, pool=None):
    """One static scene, decided like encode_v3.static_scene.  Returns codes, pool size, the background (192,224)
    and each frame (192,224) as bitmap indices."""
    import encode_poc as E, encode_v3 as V, colour as C
    bgT = E.tiles(E.oklab(bg)).astype(np.float32)
    ncel = max([int(E.tiles(c[1][:, :, None])[:, :, 0].any(1).sum()) for _, c in cels if c is not None] + [0])
    P = pool or (410 if ncel <= 410 else V.PBIG)
    V.set_layout(P)
    comps = []
    for i, cel in cels:
        if cel is None:
            comps.append((i, bgT, np.zeros((V.NT, 64), bool), np.zeros(0, int)))
            continue
        rgb, op = cel
        img = bg.copy(); img[op] = rgb[op]
        T = E.tiles(E.oklab(img)).astype(np.float32)
        cp = E.tiles(op[:, :, None])[:, :, 0]
        comps.append((i, T, cp, np.where(cp.any(1))[0]))
    sample = [bgT] + [T[c] for _, T, _, c in comps[::3] if len(c)]
    wts = [np.ones((V.NT, 64))] + [np.where(cp[c], C.CEL_WEIGHT, 1.0) for _, _, cp, c in comps[::3] if len(c)]
    S, W = np.concatenate(sample), np.concatenate(wts)
    if len(S) > 4000:
        k = np.random.default_rng(seed).choice(len(S), 4000, replace=False)
        S, W = S[k], W[k]
    codes = V.fit_palette(S, W)
    _, _, _, Bcol, smode, _, (bpx, apx, bline, aline) = V.encode_scene(bgT, codes)
    base = V.shown(bpx, apx, bline, aline, smode)
    Bidx = V.shown(bpx, apx * 0, bline, np.full(V.NT, -1), smode)
    shown_lab = shown_colours(codes, base)
    out, prev = [], {}
    for i, T, cp, cells in comps:
        img = base.copy()
        if len(cells):
            *_, disp = V.encode_cells(seamless(T[cells], cp[cells], shown_lab[cells]), cp[cells], V.CELL_IDX[cells],
                                      [int(c) for c in cells], codes, Bcol[cells], smode[cells], prev, Bidx[cells])
            img[cells] = disp
        else:
            prev.clear()
        out.append(untile(img, V.TW))
    return codes, P, untile(base, V.TW), out


def scroll_bitmaps(clip, first, last, frames, fidx=None):
    """One scrolling scene, decided like encode_scroll.scroll_scene.  Returns codes, the virtual background
    (Hg,224) and each frame's window (192,224) as bitmap indices, and the camera lines."""
    import encode_poc as E, encode_v3 as V, encode_scroll as ES, colour as C, scroll_src as S
    V.set_layout(410)
    track = S.camera_track(clip, first, last)
    vimg, top = S.virtual_bg(clip, first, last, set(fidx) if fidx else None)
    virt = ES.scale_virtual(vimg)
    Hg = len(virt)
    n = len(frames)
    fidx = fidx or list(range(first, first + n))        # the clip frames shown (a death scene's opening frames
    cam = [int(np.clip(round((track[f][1] - top) * ES.GROW), 0, Hg - E.WIN_H)) for f in fidx]   # can sit between)
    K = (16 - cam[0]) % 256
    vlab = E.oklab(virt).astype(np.float32)
    bands = range(K // 8, (Hg - 1 + K) // 8 + 1)
    bandT = {b: E.tiles(ES.band_rows(vlab, b, K)).astype(np.float32) for b in bands}
    comps = [None if cel is None else (E.oklab(cel[0]).astype(np.float32), cel[1]) for _, _, _, cel in frames]
    sample, wts = [bandT[b] for b in bands], [np.ones((V.TW, 64)) for b in bands]
    for i in range(0, n, 3):
        if comps[i] is not None:
            lab, op = comps[i]
            Tc = E.tiles(lab); cp = E.tiles(op[:, :, None])[:, :, 0]; m = cp.any(1)
            sample.append(Tc[m]); wts.append(np.where(cp[m], C.CEL_WEIGHT, 1.0))
    Sm, W = np.concatenate(sample), np.concatenate(wts)
    if len(Sm) > 4000:
        k = np.random.default_rng(0).choice(len(Sm), 4000, replace=False)
        Sm, W = Sm[k], W[k]
    codes, _ = ES.ensure_black(V.fit_palette(Sm, W))
    enc = {b: ES.encode_band(bandT[b], codes, b % 32) for b in bands}
    vdisp = np.zeros((Hg, E.WIN_W), np.uint8)
    Bidx, band_lab = {}, {}
    for b in bands:
        _, _, _, _, smode, (bpx, apx, bline, aline) = enc[b]
        rows = untile(V.shown(bpx, apx, bline, aline, smode), V.TW)
        Bidx[b] = V.shown(bpx, apx * 0, bline, np.full(V.TW, -1), smode)
        band_lab[b] = shown_colours(codes, V.shown(bpx, apx, bline, aline, smode))
        for r in range(8):
            v = 8 * b - K + r
            if 0 <= v < Hg:
                vdisp[v] = rows[r]
    needed = lambda c: range((c + K) // 8, (c + E.WIN_H - 1 + K) // 8 + 1)
    out, prev = [], {}
    for i in range(n):
        c = cam[i]
        img = vdisp[c:c + E.WIN_H].copy()
        T, cel, widx, keys, Bc, sm, Bi = [], [], [], [], [], [], []
        if comps[i] is not None:
            lab, op = comps[i]
            for b in needed(c):
                v = np.arange(8 * b - K, 8 * b - K + 8)
                r = v - c
                inwin = (r >= 0) & (r < E.WIN_H)
                if not inwin.any():
                    continue
                rr = np.clip(r, 0, E.WIN_H - 1)
                bl = ES.band_rows(vlab, b, K).copy()
                o = op[rr] & inwin[:, None]
                bl[o] = lab[rr][o]
                Tb, Ob = E.tiles(bl), E.tiles(o[:, :, None])[:, :, 0]
                _, _, _, Bcol, smode, _ = enc[b]
                Tb = seamless(Tb, Ob, band_lab[b])
                for col in np.where(Ob.any(1))[0]:
                    T.append(Tb[col]); cel.append(Ob[col]); widx.append((b % 32) * 32 + col + 2)
                    keys.append((b, int(col))); Bc.append(Bcol[col]); sm.append(smode[col]); Bi.append(Bidx[b][col])
        if T:
            *_, disp = V.encode_cells(np.array(T, np.float32), np.array(cel), np.array(widx), keys, codes,
                                      np.array(Bc, np.float32), np.array(sm), prev, np.array(Bi))
            for (b, col), px in zip(keys, disp):
                px = px.reshape(8, 8)
                for r8 in range(8):
                    r = 8 * b - K + r8 - c
                    if 0 <= r < E.WIN_H:
                        img[r, col * 8:col * 8 + 8] = px[r8]
        else:
            prev.clear()
        out.append(img)
    return codes, vdisp, out, cam


def unit_bitmaps(job):
    try:
        return unit_bitmaps_(job)
    except Exception as e:
        import traceback
        return f'{job[1]}_{job[2]}', 'FAILED: ' + ' | '.join(''.join(traceback.format_exception(e)[-3:]).splitlines())


def unit_bitmaps_(job):
    """Step 2 for one unit: job = (stage, clip, path, frame indices, force)."""
    stage, clip, path, idx, force = job
    sys.path.insert(0, os.path.join(HERE, '..', 'bd13', '3do'))
    import encode_poc as E, encode_v3 as V, scroll_src as S, scd_format as F, scd_pack as SP
    name = f'{clip}_{path}'
    d = os.path.join(OUT, stage, name)
    if os.path.exists(os.path.join(d, 'unit.json')) and not force:
        return name, 'kept (already converted)'
    t0 = time.time()
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if f.endswith('.bmp'):
            os.remove(os.path.join(d, f))
    frames = V.load_frames(clip, idx=idx)
    n = len(frames)
    track = S.camera_track(clip, min(idx), max(idx))
    cam = [track.get(f, (0, 0)) for f in idx]
    starts = [i for i, fr in enumerate(frames) if fr[1]] + [n]
    if starts[0] != 0:
        starts = [0] + starts
    jobs_ = []                              # (first, end, background and cels for a static scene, or None: scroll)
    if clip in WHOLE:                       # one layer: every frame whole and smaller, one still scene per shot
        sh = [shrink(w) for w in whole_frames(clip, idx, frames, cam, starts)]
        pic = shrink_mask()
        jobs_ = [(a, b, (sh[a], [(i, (sh[i], pic)) for i in range(a, b)], WHOLE_POOL)) for a, b in zip(starts, starts[1:])]
        starts = [n]                            # (nothing for the loop below)
    for a, b in zip(starts, starts[1:]):
        if len({c[0] for c in cam[a:b]}) > 1 or (clip in WHOLE and len(set(cam[a:b])) > 1):
            jobs_ += pan_scenes(clip, idx, frames, cam, a, b)
        elif len(set(cam[a:b])) > 1:
            jobs_.append((a, b, None))
        else:
            jobs_.append((a, b, (frames[a][2], [(i, frames[i][3]) for i in range(a, b)])
                          + ((WHOLE_POOL,) if clip in WHOLE else ())))
    scenes = []
    for k, (a, b, still) in enumerate(jobs_):
        bgname = f's{k:02d}_bg.bmp'
        if still is None:
            codes, bgimg, imgs, camv = scroll_bitmaps(clip, idx[a], idx[b - 1], frames[a:b], idx[a:b])
            pal = F.palette(codes)
            _, bgimg, imgs = SP.pack_scroll(bgimg, camv, imgs, pal, {})       # exactly what step 3 will play
            full = np.zeros((len(bgimg), F.SCREEN_W), np.uint8)
            full[:, F.WX:F.WX + E.WIN_W] = F.black16(bgimg)
            F.write_bmp(os.path.join(d, bgname), full, pal)
            scenes.append({'kind': 'scroll', 'first': a, 'frames': b - a, 'background': bgname,
                           'cels': F.cel_pattern(k), 'camera': camv})
            under = [bgimg[c:c + E.WIN_H] for c in camv]          # the background under each frame's window
        else:
            codes, P, bgimg, imgs = static_bitmaps(*still[:2], a, *still[2:])
            pal = F.palette(codes)
            _, bgimg, imgs = SP.pack_static(bgimg, imgs, pal, P, {})          # exactly what step 3 will play
            F.write_bmp(os.path.join(d, bgname), F.screen(F.black16(bgimg)), pal)
            scenes.append({'kind': 'static', 'first': a, 'frames': b - a, 'background': bgname,
                           'cels': F.cel_pattern(k), 'pool': P})
            under = [bgimg] * len(imgs)
        for j, (img, u) in enumerate(zip(imgs, under)):
            F.write_bmp(os.path.join(d, F.cel_file(scenes[-1], a + j)), F.screen(F.cel_layer(img, u)), pal)
    w = wave.open(os.path.join(E.clip_dir(clip), f'audio_{path}.wav'))
    x = np.frombuffer(w.readframes(w.getnframes()), '<i2') / 32768
    F.write_audio(os.path.join(d, 'audio.wav'), audio_levels(x, w.getframerate(), n))
    json.dump({'unit': name, 'clip': clip, 'path': path, 'frames': n, 'audio': 'audio.wav', 'scenes': scenes},
              open(os.path.join(d, 'unit.json'), 'w'), indent=1)
    return name, f'{n} frames, {len(scenes)} scenes ({time.time() - t0:.0f} s)'


def shrink(w):
    """A window picture (192,224,3) at WHOLE_SIZE, centred on black."""
    from PIL import Image
    pw, ph = WHOLE_SIZE
    H, W = w.shape[:2]
    out = np.zeros_like(w)
    x0, y0 = (W - pw) // 16 * 8, (H - ph) // 16 * 8
    img = Image.fromarray(np.clip(np.round(w), 0, 255).astype(np.uint8)).resize((pw, ph), Image.LANCZOS)
    out[y0:y0 + ph, x0:x0 + pw] = np.asarray(img, w.dtype)
    return out


def shrink_mask():
    import encode_poc as E
    pw, ph = WHOLE_SIZE
    m = np.zeros((E.WIN_H, E.WIN_W), bool)
    x0, y0 = (E.WIN_W - pw) // 16 * 8, (E.WIN_H - ph) // 16 * 8
    m[y0:y0 + ph, x0:x0 + pw] = True
    return m


def whole_frames(clip, idx, frames, cam, starts):
    """Every frame of a clip as one picture (192,224,3): its background at its camera, with its cel over it."""
    import encode_poc as E, scroll_src as S
    from PIL import Image
    out = []
    for a, b in zip(starts, starts[1:]):
        if len(set(cam[a:b])) > 1:
            img, left, top = S.virtual_bg2d(clip, idx[a], idx[b - 1], set(idx[a:b]))
            H, W = img.shape[:2]
        for i in range(a, b):
            if len(set(cam[a:b])) > 1:
                x, y = cam[i]
                x0, y0 = int(np.clip(x - left, 0, W - 288)), int(np.clip(y - top, 0, H - 216))
                w = E.scale_rgb(Image.fromarray(img[y0:y0 + 216, x0:x0 + 288]))
            else:
                w = frames[a][2].copy()
            if frames[i][3] is not None:
                rgb, op = frames[i][3]
                w[op] = rgb[op]
            out.append(w)
    return out


def pan_scenes(clip, idx, frames, cam, a, b):
    """A scene whose camera moves sideways (the player only scrolls vertically): the still part before the pan,
    the pan, and the still part after it, each a static scene.  The still parts are exact (their background is
    the virtual background at that camera position); in the pan every frame is the whole window, as a cel over
    the pan's first frame (frame cells change everywhere, so similar tiles get merged to fit the frame pool).
    Returns [(first, end, (background, [(frame, cel)]))]."""
    import encode_poc as E, scroll_src as S
    from PIL import Image
    img, left, top = S.virtual_bg2d(clip, idx[a], idx[b - 1], set(idx[a:b]))
    H, W = img.shape[:2]

    def window(i):
        x, y = cam[i]
        x0, y0 = int(np.clip(x - left, 0, W - 288)), int(np.clip(y - top, 0, H - 216))
        return E.scale_rgb(Image.fromarray(img[y0:y0 + 216, x0:x0 + 288]))

    def comp(i):
        w = window(i)
        if frames[i][3] is not None:
            rgb, op = frames[i][3]
            w[op] = rgb[op]
        return w
    moving = [i for i in range(a + 1, b) if cam[i] != cam[i - 1]]
    m0, m1 = moving[0], moving[-1] + 1                    # the pan: frames m0 .. m1-1
    if clip in WHOLE:                                     # (no still scenes of a frame or two round it: each would
        m0, m1 = a, b                                     # load a background of its own)
    out = []
    if m0 > a:
        out.append((a, m0, (window(a), [(i, frames[i][3]) for i in range(a, m0)])))
    allop = np.ones((E.WIN_H, E.WIN_W), bool)
    out.append((m0, m1, (comp(m0), [(i, (comp(i), allop)) for i in range(m0, m1)],
                         WHOLE_POOL if clip in WHOLE else PAN_POOL)))
    if m1 < b:
        out.append((m1, b, (window(m1), [(i, frames[i][3]) for i in range(m1, b)])))
    return out


def audio_levels(x, rate, nframes):
    """Mono audio -> nframes * 1200 samples at 14.4 kHz, -126..126 (as encode_v3.audio_bytes, before the
    sign-magnitude coding): resampled in the frequency domain, nothing above 7.2 kHz folds back."""
    import encode_poc as E
    n_src = int(round(nframes * rate / E.FPS))
    x = np.pad(np.asarray(x, float)[:n_src], (0, max(0, n_src - len(x))))
    n_out = nframes * E.AUD_PER_FRAME
    X = np.fft.rfft(x)[:n_out // 2 + 1]
    X[int(len(X) * 0.95):] = 0
    y = np.fft.irfft(X, n_out) * n_out / n_src
    return np.clip(np.rint(y * 127), -126, 126).astype(int)


LABEL_SELECTED = (0, 255, 255)          # the labels' own colour in the 3DO cels (its PLUT entry 0, set at run time)


def top_up(T, codes):
    """Flat artwork uses few colours: fill the lines' free entries with the grid colours that cells miss most
    (each cell is served by its best pair of lines), so flat areas come out solid instead of dithered."""
    import collections
    import encode_v3 as V, colour as C
    G = C.snap(T)                                                   # grid code of every pixel
    codes = [list(c) for c in codes]
    while True:
        pr, _ = V.best_pair(V.dists2(T, [np.array(c) for c in codes])[0])
        need = collections.Counter()
        for c in range(len(T)):
            a, b = pr[c]
            have = set(codes[a]) | set(codes[b])
            room = [l for l in (a, b) if len(codes[l]) < 15]
            if not room:
                continue
            l = min(room, key=lambda l: len(codes[l]))
            for g in G[c]:
                if g not in have:
                    need[(l, int(g))] += 1
        if not need:
            return [np.array(c) for c in codes]
        (l, g), n = need.most_common(1)[0]
        if n < 4:                                                   # stray pixels: not worth an entry
            return [np.array(c) for c in codes]
        codes[l].append(g)


def menu_bitmaps():
    """The pause overlay and the save menu (from step 1's extracted/menus):
      scd/MENUS/pause.bmp      the PAUSE overlay as a sprite: index 1 = fill, 2 = outline, 0 = transparent; drawn
                               at run time in the lightest / darkest colours of the paused scene's palette
      scd/MENUS/SAVEMENU/      the save menu as a one-frame unit (scd_format.py).  Palette line 3 is reserved:
                               colours 1-3 are the three button labels (unit.json "menu"), recoloured at run time
                               to show the selection; lines 0-2 hold the background, every cell shown normally."""
    import encode_poc as E, encode_v3 as V, scd_format as F, scd_pack as SP
    from PIL import Image
    src = os.path.join(SRC, 'menus')
    out = os.path.join(OUT, 'MENUS'); os.makedirs(out, exist_ok=True)
    # pause overlay
    rgb, op = E.scale_cel(Image.open(os.path.join(src, 'PAUSE.png')))
    spr = np.where(op, np.where(rgb.mean(2) >= 128, 1, 2), 0).astype(np.uint8)
    pal = [F.TRANSPARENT, (255, 255, 255), (0, 0, 0)] + [F.UNUSED] * 253
    F.write_bmp(os.path.join(out, 'pause.bmp'), F.screen(spr), pal)
    # save menu: the 3DO picture 1:1 on the H40 screen; line 3 colours 1-3 = the three labels
    lay = json.load(open(os.path.join(src, 'savegame.json')))
    masks = [wide_mask(os.path.join(src, b['label'])) for b in lay['buttons']]
    pal, img = wide_screen(os.path.join(src, lay['background']), masks, [0] * len(masks))
    d = os.path.join(out, 'SAVEMENU')
    acts = lay['actions']
    write_wide_unit(d, 'SAVEMENU', img, pal, {'menu': {
        'items': [{'action': acts[str(b['action'])], 'colour': 48 + 1 + k} for k, b in enumerate(lay['buttons'])],
        'label': [0, 0, 0], 'selected': list(LABEL_SELECTED)}})
    title_bitmaps(out)
    cheat_bitmaps(out, force=True)
    print('menus in', out)


def wide_canvas(path):
    """A 3DO 288x216 picture 1:1 on the 320x224 H40 screen (at x 16, y 4; black round it), as RGB floats."""
    import scd_format as F
    from PIL import Image
    c = np.zeros((F.WIDE_H, F.WIDE_W, 3), np.float32)
    c[F.WIDE_Y:F.WIDE_Y + 216, F.WIDE_X:F.WIDE_X + 288] = np.array(Image.open(path).convert('RGB'), np.float32)
    return c


def wide_mask(path, keep=0.5):
    """The opaque pixels of a 3DO 288x216 cel on the H40 screen."""
    import scd_format as F
    from PIL import Image
    m = np.zeros((F.WIDE_H, F.WIDE_W), bool)
    m[F.WIDE_Y:F.WIDE_Y + 216, F.WIDE_X:F.WIDE_X + 288] = np.array(Image.open(path).convert('RGBA'))[:, :, 3] >= keep * 255
    return m


def wide_screen(path, masks, codes3):
    """A menu screen for H40 (scd_format: kind 'wide'): the 3DO picture 1:1, lines 0-2 fitted to it (flat artwork:
    topped up, see top_up), normal or shadowed per cell, pattern dithered; then each mask's pixels become line 3
    colour 1, 2, ... (labels, outlines) with their cells shown normally on one background line.  codes3: the
    grid codes of those line 3 colours.  Returns (palette, indices (224,320)), exactly as step 3 will store them."""
    import encode_poc as E, encode_v3 as V, colour as C, scd_format as F, scd_pack as SP
    rgb = wide_canvas(path)
    T = E.tiles(E.oklab(rgb)).astype(np.float32)
    n = len(T)
    codes = top_up(T, V.fit_palette(T, np.ones((n, 64)), nl=3))
    D2 = V.dists2(T, codes)
    cost = [np.minimum(D2[m].sum(1).min(1), V.best_pair(D2[m])[1]) for m in (0, 1)]
    mode = (cost[1] < cost[0]).astype(int)
    D = V.pick(D2, mode)
    e1 = D.sum(1); l1 = e1.argmin(1)
    pr, e2 = V.best_pair(D)
    up = e2 < e1[np.arange(n), l1]
    pbl, pal_ = V.pair_lines(D, np.arange(n), pr, e1)
    bl, al = np.where(up, pbl, l1), np.where(up, pal_, -1)
    th = np.broadcast_to(C.THRESH[0], (n, 64))
    bpx, apx = V.dither_cells(T, np.zeros((n, 64), bool), th, codes, bl, al, mode)
    cells = V.shown(bpx, apx, bl, np.where(up & apx.any(1), al, -1), mode)
    pal = F.palette(list(codes) + [np.array(codes3, int)])
    P = SP.Pal(pal)
    text = E.tiles(np.any(masks, 0)[:, :, None])[:, :, 0].any(1) if masks else np.zeros(n, bool)
    for c in np.where(text)[0]:          # label cells: shown normally (true label colours), background on its
        lab = P.lab[cells[c]]            # best single line, line 3 above it
        best = min(range(3), key=lambda l: ((lab[:, None] - P.lab[None, l * 16 + 1:l * 16 + 16]) ** 2).sum(2).min(1).sum())
        cells[c] = best * 16 + 1 + ((lab[:, None] - P.lab[None, best * 16 + 1:best * 16 + 16]) ** 2).sum(2).argmin(1)
    img = cells.reshape(F.WIDE_H // 8, F.WIDE_W // 8, 8, 8).swapaxes(1, 2).reshape(F.WIDE_H, F.WIDE_W)
    for k, m in enumerate(masks):
        img[m] = 48 + 1 + k
    _, img = SP.pack_wide(img, pal, {})                                    # exactly what step 3 will store
    return pal, img


def write_wide_unit(d, name, img, pal, extra):
    """A one-frame unit of kind 'wide' (scd_format.py): s00_bg.bmp 320x224, an empty cel layer, silence."""
    import scd_format as F
    os.makedirs(d, exist_ok=True)
    F.write_bmp(os.path.join(d, 's00_bg.bmp'), F.black16(img), pal, wide=True)
    sc = {'kind': 'wide', 'first': 0, 'frames': 1, 'background': 's00_bg.bmp', 'cels': F.cel_pattern(0)}
    F.write_bmp(os.path.join(d, F.cel_file(sc, 0)), np.zeros_like(img), pal, wide=True)
    F.write_audio(os.path.join(d, 'audio.wav'), np.zeros(1200, int))
    json.dump(dict({'unit': name, 'frames': 1, 'audio': 'audio.wav', 'scenes': [sc]}, **extra),
              open(os.path.join(d, 'unit.json'), 'w'), indent=1)


def title_bitmaps(out):
    """The start screen (3DO SELECT.BIN / SELECTBG) for H40: scd/MENUS/TITLE/ - a one-frame wide unit (line 3
    colours 1-2: the selection outline's blue and white, used by its sprites) and outline_<button>.bmp per button
    (the outline the 3DO draws round the selected button, 1:1, line 3 colours 1/2, shown as sprites at run time).
    unit.json "title": the buttons in order, their action and clip."""
    import scd_format as F
    from PIL import Image
    src = os.path.join(SRC, 'menus')
    lay = json.load(open(os.path.join(src, 'select.json')))
    blue, white = F.rgb_code((0, 0, 255)), F.rgb_code((255, 255, 255))
    pal, img = wide_screen(os.path.join(src, lay['background']), [], [blue, white])
    d = os.path.join(out, 'TITLE')
    buttons = []
    os.makedirs(d, exist_ok=True)
    for b in lay['buttons']:
        a = np.array(Image.open(os.path.join(src, b['outline'])).convert('RGBA'))
        ol = np.zeros((F.WIDE_H, F.WIDE_W), np.uint8)
        ol[F.WIDE_Y:F.WIDE_Y + 216, F.WIDE_X:F.WIDE_X + 288] = np.where(
            a[:, :, 3] > 0, np.where(a[:, :, 0] >= 128, 48 + 2, 48 + 1), 0)
        name = 'outline_' + b['outline'][:-4] + '.bmp'
        F.write_bmp(os.path.join(d, name), ol, pal, wide=True)
        buttons.append({'action': b['action'], 'clip': b.get('clip'), 'outline': name})
    write_wide_unit(d, 'TITLE', img, pal, {'title': buttons})


def cheat_bitmaps(out, force=False):
    """The cheat menu (the 3DO test menu: Up Down Up Down Up Down Up Down, Left on the start screen; TESTBACK.RAW and
    SELTEST.BIN) for H40: scd/MENUS/CHEATMENU/ - a one-frame wide unit, the 3DO picture 1:1 (line 3 colour 1: the
    box yellow, used by its sprites), and box_<map>.bmp per entry: the yellow box the 3DO draws round the selected
    entry (its cel, 1:1, line 3 colour 1), shown as sprites at run time.  unit.json "cheat": the entries in the 3DO
    order (across each row: full test, deaths, no deaths), their map (<map>.BIN, a cheat map) and box.
    Kept if it is already there, unless force (python scd_bitmaps.py CHEAT --force)."""
    import scd_format as F
    from PIL import Image
    d = os.path.join(out, 'CHEATMENU')
    if os.path.exists(os.path.join(d, 'unit.json')) and not force:
        print('kept', d)
        return
    src = os.path.join(SRC, 'menus')
    lay = json.load(open(os.path.join(src, 'seltest.json')))
    pal, img = wide_screen(os.path.join(src, lay['background']), [], [F.rgb_code((255, 247, 0))])
    pal = list(pal)
    for i in np.unique(img):                    # the text yellow as the box yellow (the fit leans it to blue 52)
        if i < 48 and pal[i][0] > 200 and pal[i][1] > 200:
            pal[i] = pal[49]
    os.makedirs(d, exist_ok=True)
    entries = []
    for e in lay['entries']:
        a = np.array(Image.open(os.path.join(src, e['box'])).convert('RGBA'))
        box = np.zeros((F.WIDE_H, F.WIDE_W), np.uint8)
        box[F.WIDE_Y:F.WIDE_Y + 216, F.WIDE_X:F.WIDE_X + 288] = np.where(a[:, :, 3] > 0, 48 + 1, 0)
        name = f"box_{e['map']}.bmp"
        F.write_bmp(os.path.join(d, name), box, pal, wide=True)
        entries.append({'map': e['map'], 'box': name})
    write_wide_unit(d, 'CHEATMENU', img, pal, {'cheat': entries})
    print('cheat menu in', d)


def jobs(stage, only=(), force=False):
    """stage: BD04, or ALL (logic_ALL.json; each unit goes to its own stage's folder, scd/BD20/...)."""
    logic = json.load(open(os.path.join(SRC, f'logic_{stage}.json')))
    out = []
    for clip in sorted(logic):
        for path in sorted(logic[clip]['paths'], key=lambda p: (p != 'main', p)):
            if not only or f'{clip}_{path}' in only:
                out.append((clip[:4], clip, path, logic[clip]['paths'][path], force))
    return out


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    workers = int(sys.argv[sys.argv.index('--workers') + 1]) if '--workers' in sys.argv else 4
    if '--workers' in sys.argv:
        args.remove(str(workers))
    stage, only = args[0].upper(), set(args[1:])
    if stage == 'MENUS':
        menu_bitmaps(); sys.exit()
    if stage == 'CHEAT':
        cheat_bitmaps(os.path.join(OUT, 'MENUS'), '--force' in sys.argv); sys.exit()
    todo = jobs(stage, only, '--force' in sys.argv)
    t0 = time.time()
    with Pool(workers) as p:
        for name, msg in p.imap_unordered(unit_bitmaps, sorted(todo, key=lambda j: -len(j[3]))):
            print(f'  {name}: {msg} ({time.time() - t0:.0f} s)', flush=True)
    print('bitmaps in', os.path.join(OUT, stage))

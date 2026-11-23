"""Step 1 of 3 - export a stage from the 3DO discs:  python export_3do.py BD04|ALL [--no-images]

Writes everything later steps need, in ordinary formats, so nothing after this reads the disc:
  extracted/disc1/<clip>/f<frame>_<CHUNK>.png   every bitmap chunk (extract_bd13.py: BACK/BAC8 backgrounds,
                                                MVBG/MVB8 strips, CL16/CELL cels with alpha), full 3DO quality
  extracted/manifest.csv                        the chunks and INFO line of every frame
  extracted/logic_<stage>.json                  paths, decisions, jumps (logic.py)
  extracted/disc<n>/<clip>/audio_<path>.wav       each path's own soundtrack (snd3do.py), 16-bit mono 22050 Hz
  extracted/disc<n>/<clip>/camera.json            the camera position (MOVE chunk: x, y) of every frame
  extracted/cheat/                              BEEPS.BIN (the button sounds) and the cheat maps (<stage>TEST/DEATH/
                                                CHEAT.BIN: the game's own test runs, see segacd/cheat_script.py)
  extracted/menus/                              the pause overlay (PAUSE.png) and the save menu: its background
                                                (SAVEBACK.png), button labels (SAVESAVE/SAVECONT/SAVEEXIT.png) and
                                                layout (savegame.json, from SAVEGAME.BIN: label, action number)

Next: segacd/scd_bitmaps.py (step 2: Sega CD quality bitmaps), then segacd/scd_pack.py (step 3: the
binaries the player reads).
"""
import json, os, struct, sys, wave
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_bd13, logic, opera, snd3do

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'extracted')


def write_wav(path, x, rate):
    w = wave.open(path, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes((np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()); w.close()


def export_menus():
    """The pause overlay and the save menu (stage independent)."""
    from PIL import Image
    d = os.path.join(OUT, 'menus'); os.makedirs(d, exist_ok=True)
    disc = opera.Disc(extract_bd13.DISCS[0])
    files = {n.upper(): (p, nb) for t, n, p, nb, *_ in disc.tree() if t == 2}
    for n in ('PAUSE.CEL', 'SAVESAVE.CEL', 'SAVECONT.CEL', 'SAVEEXIT.CEL'):
        Image.fromarray(extract_bd13.menu_cel(disc.read(*files[n]))).save(os.path.join(d, n[:-4] + '.png'))
    raw = disc.read(*files['SAVEBACK.RAW'])
    Image.fromarray(extract_bd13.deint(extract_bd13.rgb555(np.frombuffer(raw, '>u2').reshape(108, 576)))).save(
        os.path.join(d, 'SAVEBACK.png'))
    # SAVEGAME.BIN: u16 count, background name (14 bytes), then per button u16 action + name (14 bytes)
    b = disc.read(*files['SAVEGAME.BIN'])
    n = struct.unpack('>H', b[:2])[0]
    items, p = [], 16
    for _ in range(n):
        act = struct.unpack('>H', b[p:p + 2])[0]
        items.append({'label': b[p + 2:p + 16].rstrip(b'\0').decode()[:-4] + '.png', 'action': act})
        p += 16
    json.dump({'background': b[2:16].rstrip(b'\0').decode()[:-4] + '.png', 'buttons': items,
               'actions': {'0': 'continue', '1': 'save', '2': 'exit'}}, open(os.path.join(d, 'savegame.json'), 'w'),
              indent=1)
    # the start screen: SELECT.BIN (as SELECT.SEL says): background, then per button u16 value + its outline cel
    # (14 bytes); value > 0 = the clip New Game starts, 0 = Continue a saved game, < 0 = demo sequence -n (the
    # trailers BD96, BD51, BD53; -1 = BD01, the intro)
    raw = disc.read(*files['SELECTBG.RAW'])
    Image.fromarray(extract_bd13.deint(extract_bd13.rgb555(np.frombuffer(raw, '>u2').reshape(108, 576)))).save(
        os.path.join(d, 'SELECTBG.png'))
    b = disc.read(*files['SELECT.BIN'])
    n = struct.unpack('>H', b[:2])[0]
    items, p = [], 16
    for _ in range(n):
        v = struct.unpack('>h', b[p:p + 2])[0]
        name = b[p + 2:p + 16].rstrip(b'\0').decode()
        Image.fromarray(extract_bd13.menu_cel(disc.read(*files[name.upper()]))).save(os.path.join(d, name[:-4] + '.png'))
        if v > 0:
            act = {'action': 'new', 'clip': f'BD{v // 1000:02d}_{v % 1000:03d}'}
        elif v == 0:
            act = {'action': 'continue'}
        else:
            act = {'action': 'demo', 'clip': f'BD{-v:02d}_001'}
        items.append(dict(outline=name[:-4] + '.png', value=v, **act))
        p += 16
    json.dump({'background': 'SELECTBG.png', 'buttons': items}, open(os.path.join(d, 'select.json'), 'w'), indent=1)
    # the test menu (the cheat code on the start screen opens it): SELTEST.BIN as SELECT.BIN, u16 count, background
    # (14 bytes), then per entry u16 (0) + its box cel (14 bytes); the entry plays <name>.BIN, the map of the same name.
    # GDNDEATH.CEL is on neither disc: its entry has no box (box None)
    raw = disc.read(*files['TESTBACK.RAW'])
    Image.fromarray(extract_bd13.deint(extract_bd13.rgb555(np.frombuffer(raw, '>u2').reshape(108, 576)))).save(
        os.path.join(d, 'TESTBACK.png'))
    b = disc.read(*files['SELTEST.BIN'])
    n = struct.unpack('>H', b[:2])[0]
    items, p = [], 16
    for _ in range(n):
        name = b[p + 2:p + 16].rstrip(b'\0').decode()
        box = None
        if name.upper() in files:
            box = name[:-4] + '.png'
            Image.fromarray(extract_bd13.menu_cel(disc.read(*files[name.upper()]))).save(os.path.join(d, box))
        items.append(dict(map=name[:-4], box=box))
        p += 16
    json.dump({'background': 'TESTBACK.png', 'entries': items}, open(os.path.join(d, 'seltest.json'), 'w'), indent=1)
    print('menus:', d)
    # the button sounds and the game's own test runs (cheat maps), for the player and for test scripts
    c = os.path.join(OUT, 'cheat'); os.makedirs(c, exist_ok=True)
    for n, (p, nb) in files.items():
        if n == 'BEEPS.BIN' or (n.endswith(('TEST.BIN', 'DEATH.BIN', 'CHEAT.BIN')) and '/' not in n):
            open(os.path.join(c, n), 'wb').write(disc.read(p, nb))


def find_mirrors(lg):
    """extracted/mirrors.json: clips that are mirror images of another clip (the 3DO stores mirrored scenes as
    clips of their own, e.g. BD43 = BD34, BD31 = BD30, BD42 = BD41 mirrored): their first backgrounds match when
    one is flipped left to right (black or near-flat backgrounds are left out)."""
    from PIL import Image
    first = {}
    for clip, L in lg.items():
        d = os.path.join(OUT, f'disc{L["disc"]}', clip)
        bgs = sorted((n[5:10], n) for n in os.listdir(d) if n.startswith(('BACK_', 'BAC8_')))
        if bgs:
            a = np.array(Image.open(os.path.join(d, bgs[0][1])).convert('RGB').resize((72, 54))).astype(float)
            if a.std() > 12:
                first[clip] = a
    names = sorted(first)
    nfr = lambda c: sum(len(v) for v in lg[c]['paths'].values())
    best = {}
    for a in names:                      # several clips can open on the same background: the partner is the one
        cand = [b for b in names if b != a and np.abs(first[a][:, ::-1] - first[b]).mean() < 3]
        if cand:                         # with the same clip number, then the nearest length
            best[a] = min(cand, key=lambda b: (b[5:] != a[5:], abs(nfr(a) - nfr(b)), b))
    pairs = sorted({tuple(sorted((a, b))) for a, b in best.items() if best.get(b) == a})
    json.dump([{'clip': a, 'mirror_of': b, 'frames': [nfr(a), nfr(b)]} for b, a in pairs],
              open(os.path.join(OUT, 'mirrors.json'), 'w'), indent=1)
    return pairs


def export(stage, images=True):
    """stage: BD04, or ALL for every stage of both discs."""
    export_menus()
    lg, lpath = logic.all_logic() if stage == 'ALL' else logic.stage_logic(stage)
    print('logic:', lpath)
    if images:
        extract_bd13.main(OUT, set(lg))
    files = logic.clip_files()
    for clip, L in lg.items():
        dn, disc, ptr, nb = files[clip]
        d = os.path.join(OUT, f'disc{dn}', clip)
        cam = []
        for _, ch in extract_bd13.frames(disc.read(ptr, nb)):
            mv = [b for tg, b in ch if tg == 'MOVE']
            cam.append(list(struct.unpack('>hh', mv[0])) if mv else [0, 0])
        json.dump(cam, open(os.path.join(d, 'camera.json'), 'w'))
        for path, idx in L['paths'].items():
            write_wav(os.path.join(d, f'audio_{path}.wav'), snd3do.path_track(clip, idx, dn), snd3do.RATE)
        print(f'{clip} (disc {dn}): camera, audio of {len(L["paths"])} path(s)', flush=True)
    print('mirrored clips:', len(find_mirrors(lg)), 'pairs (extracted/mirrors.json)')


if __name__ == '__main__':
    export(sys.argv[1].upper(), '--no-images' not in sys.argv)

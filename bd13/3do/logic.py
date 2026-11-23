"""Game logic of the 3DO BrainDead 13 clips:  python logic.py BD04   -> extracted/logic_BD04.json

What the chunks mean (worked out from the data):
  * A clip file holds a main path (INFO scene numbers below 500) and death scenes (5xx).  A death scene's head
    (1-12 frames, with its own background, audio preload FSND and REVV) is stored just before the decision that
    can lead to it, so the 3DO can cut to it at once; its tail is at the end of the file.  When playing a path, the
    other paths' frames are skipped.  Every path is self-contained: its frames' sound chunks form its audio
    (15 frames preloaded, 14 frames ahead of the video at the end - true for every path of every clip).
  * EVNT (16 bytes): u16 frame (1-based, in the main path), u8 window (12 frames = 1 s at 12 fps), u8 flags, then
    6 outcomes in the order no input, up, down, left, right, fire.  The order is confirmed by the DOS version's
    cheat maps (CD root CHEATFIL.TXT / X*.BIN, see bd13/dos/cheat): the "no deaths" BD04 run X6.BIN is exactly the
    right moves and routes of these EVNTs.  The DOS BD.DAT has the same decisions as type-3 records at the same
    frames.
      flags 0x00: outcome 0 = the right move, otherwise the death scene number (a path in this file).  No input in
                  the window is outcome 0's slot, so waiting is usually a death too.
      flags 0x40: outcomes are clip numbers (4004 = BD04_004): a route choice; 0 = not offered (the press is
                  ignored).  A chosen route takes over 25 frames after the window ends (DOS stores this switch
                  point as an offset: always decision frame + 37); with no choice the clip plays on into a
                  death for waiting, then the revive clip and this clip again.
      flags 0x20 (52 bytes, BD04_014/015): the stage exit.  Directions lead to the first clip of other stages;
                  no input / fire (value 9) pick from a list of 9 death clips (e.g. BD36_500..580).
  * JUMP (u16 clip number): the clip that follows when the main path ends.  It sits 25 frames before the end (more
    in BD04_013): the 3DO starts seeking there, but the frames after it are still shown.
  * REVV (0, 4900): after a death scene, play clip 4900 (the revive scene), then restart this file's main path.
  * Not used yet: HOTS (empty in BD04), COND (clip lists, maybe stages already visited), HOLD, PURG.
Outputs per clip: its paths (name, frame indices), decisions (path frame, window, outcomes), the jump, COND
rules and the disc it is on.   python logic.py ALL   decodes every stage of both discs (+ logic_ALL.json).
"""
import json, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opera
from extract_bd13 import DISCS, frames

INPUTS = ['none', 'up', 'down', 'left', 'right', 'fire']
ROUTE_LEAD = 25         # a chosen route takes over this many frames after its window (DOS BD.DAT: the decision's offset)
clipname = lambda v: f'BD{v // 1000:02d}_{v % 1000:03d}'


def clip_logic(data):
    rows = []
    for i, (_, ch) in enumerate(frames(data)):
        info = next((b.rstrip(b'\0').decode('latin1') for tg, b in ch if tg == 'INFO'), '')
        sc = int(info.split(' ')[2][2:]) if info.count(' ') >= 3 else 0
        rows.append((i, sc, ch))
    path_of = lambda sc: 'main' if sc < 500 else str(sc // 10 * 10)
    paths = {}
    for i, sc, _ in rows:
        paths.setdefault(path_of(sc), []).append(i)
    pos = {p: {f: k for k, f in enumerate(fs)} for p, fs in paths.items()}
    out = {'paths': paths, 'decisions': [], 'jump': None, 'revive': None, 'other': []}
    for i, sc, ch in rows:
        p = path_of(sc)
        for tg, b in ch:
            if tg == 'EVNT':
                w = struct.unpack(f'>{len(b) // 2}H', b)
                frame1, window, flags = w[0], w[1] >> 8, w[1] & 0xFF
                assert pos[p][i] + 1 == frame1, (i, frame1, pos[p][i])
                kind = 'death' if flags == 0 else 'route'
                outcomes, tail, li = {}, w[8:], 0
                for name, v in zip(INPUTS, w[2:8]):
                    if kind == 'death':
                        outcomes[name] = None if v == 0 else str(v // 10 * 10)
                    elif v and len(tail) >= v * (li + 1) and v < 100:
                        # a list: v clip numbers after the six outcomes, one picked at random (deaths for a
                        # wrong way: BD20_500 ... BD20_580)
                        outcomes[name] = [clipname(x) for x in tail[v * li:v * (li + 1)]]; li += 1
                    else:
                        outcomes[name] = None if v == 0 else clipname(v)
                d = {'path': p, 'frame': pos[p][i], 'window': window, 'kind': kind, 'outcomes': outcomes}
                if kind == 'route':
                    d['flags'] = flags
                    d['cut'] = pos[p][i] + window + ROUTE_LEAD
                out['decisions'].append(d)
            elif tg == 'COND':
                # COND: target, replacement, then clip numbers: once all those clips have been visited, a route
                # outcome to the target goes to the replacement instead (0 = no longer offered)
                w = struct.unpack(f'>{len(b) // 2}H', b)
                out.setdefault('cond', []).append({'target': clipname(w[0]), 'then': clipname(w[1]) if w[1] else None,
                                                   'visited': [clipname(x) for x in w[2:]]})
            elif tg == 'JUMP':
                v = struct.unpack('>H', b)[0]
                out['jump'] = clipname(v)
                out['jump_at'] = (p, pos[p][i])         # the seek starts here; the clip still plays to its end
            elif tg == 'REVV':
                v = struct.unpack('>HH', b)
                out['revive'] = clipname(v[1])
    return out


def clip_files():
    """{clip: (disc number, disc, ptr, bytes)} for every clip on both discs; a clip on both discs is taken from
    disc 1 (the copies are the same file)."""
    files = {}
    for dn, dp in enumerate(DISCS, 1):
        disc = opera.Disc(dp)
        for t, name, ptr, nb, *_ in disc.tree():
            if t == 2 and name.upper().endswith('.DAT'):
                c = name[:-4].upper()
                if c in files:
                    DISC2_TOO.add(c)
                files.setdefault(c, (dn, disc, ptr, nb))
    return files


DISC2_TOO = set()                                   # clips on both discs


def stages():
    return sorted({c[:4] for c in clip_files()})


def stage_logic(prefix, files=None):
    """Decode every clip of a stage (BD04_*) and write extracted/logic_<prefix>.json; returns (logic, path).
    Each clip also gets the 3DO disc it is on ('disc'; 1 when it is on both)."""
    files = files or clip_files()
    res = {}
    for c in sorted(files):
        if c.startswith(prefix):
            dn, disc, ptr, nb = files[c]
            res[c] = clip_logic(disc.read(ptr, nb))
            res[c]['disc'] = dn
            res[c]['both_discs'] = c in DISC2_TOO
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extracted', f'logic_{prefix}.json')
    json.dump(res, open(out, 'w'), indent=1)
    return res, out


def all_logic():
    """Every stage (extracted/logic_<stage>.json each) and the whole game in extracted/logic_ALL.json."""
    files = clip_files()
    res = {}
    for st in sorted({c[:4] for c in files}):
        res.update(stage_logic(st, files)[0])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extracted', 'logic_ALL.json')
    json.dump(res, open(out, 'w'), indent=1)
    return res, out


if __name__ == '__main__':
    res, out = all_logic() if sys.argv[1].upper() == 'ALL' else stage_logic(sys.argv[1].upper())
    for c, r in res.items():
        print(f'{c}: paths {{{", ".join(f"{p}: {len(fs)}" for p, fs in r["paths"].items())}}} jump {r["jump"]} '
              f'revive {r["revive"]}')
        for d in r['decisions']:
            o = ' '.join(f'{k}={v}' for k, v in d['outcomes'].items() if v is not None) if d['kind'] != 'death' else                 'right move: ' + ','.join(k for k, v in d['outcomes'].items() if v is None) +                 f' (else death {next(v for v in d["outcomes"].values() if v)})'
            print(f'   {d["path"]} frame {d["frame"]} [{d["kind"]}] {o}')
        for x in r['other']:
            print('   not handled:', x)
    print('wrote', out)

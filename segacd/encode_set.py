"""Encode a stage of 3DO clips for the Sega CD player: one stream file per path, plus the game logic.

Superseded by the three-step pipeline (bd13/3do/export_3do.py, scd_bitmaps.py, scd_pack.py), which uses
units_of() and resolve() from here; running this directly still encodes straight from the 3DO frames.

  python encode_set.py BD04 [workers]   -> out/set/<clip>_<path>.bin, out/set_BD04.json (units + logic)

A clip file holds a main path and death scenes (see bd13/3do/logic.py); each becomes a unit with its own stream
(format version 5, scene records carrying an audio preroll) so the player can start any of them on its own.  A
path's scenes are split at its backgrounds; a scene whose camera moves (MOVE) is a scrolling scene.  Every path
uses its own frames' 3DO sound.  Units are encoded in parallel and cached (out/set/*.pkl).

The JSON lists the units in disc order with their logic, resolved to unit numbers:
  decisions: path frame, window, kind (death / route), outcomes per input (no input, up, down, left, right, fire;
  unit number or null = right move / not offered), cut (route: the frame where the chosen route takes over);
  next: the unit after the path ends (JUMP, or after a death - including a main path that ends in a death for not
  choosing a route - the revive clip); restart: the unit to play after the revive clip.
"""
import json, os, pickle, sys, time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'out', 'set')
LOGIC = os.path.join(HERE, '..', 'bd13', '3do', 'extracted', 'logic_{}.json')


def encode_unit(job):
    clip, path, idx = job
    name = f'{clip}_{path}'
    pk = os.path.join(CACHE, name + '.pkl')
    if os.path.exists(pk):
        return name, 'cached'
    sys.path.insert(0, os.path.join(HERE, '..', 'bd13', '3do'))
    import encode_v3 as V, encode_scroll as ES, scroll_src as S, snd3do
    t0 = time.time()
    frames = V.load_frames(clip, idx=idx)
    n = len(frames)
    track = S.camera_track(clip, min(idx), max(idx))
    cam = [track.get(f, (0, 0)) for f in idx]
    starts = [i for i, fr in enumerate(frames) if fr[1]] + [n]
    if starts[0] != 0:
        starts = [0] + starts
    recs, scenes = [], []
    for a, b in zip(starts, starts[1:]):
        if len(set(cam[a:b])) > 1:
            assert idx[a:b] == list(range(idx[a], idx[b - 1] + 1)), 'scrolling scene must be contiguous'
            r = ES.scroll_scene(clip, idx[a], idx[b - 1], frames[a:b])[0]
            kind = 'scroll'
        else:
            st = {}
            r = V.static_scene(frames[a][2], [(i, frames[i][3]) for i in range(a, b)], a, st)
            kind = st['scene'][0][4]
        recs += r
        scenes.append((a, b - a, kind))
    audio = V.audio_bytes(snd3do.path_track(clip, idx), snd3do.RATE, n)
    stream = V.write_stream(V.set_audio(recs, audio), n, len(scenes), os.path.join(CACHE, name + '.bin'))
    pickle.dump({'name': name, 'frames': n, 'scenes': scenes, 'bytes': len(stream)}, open(pk, 'wb'))
    return name, f'{n} frames, {len(scenes)} scenes, {len(stream) / 1024:.0f} KB, {time.time() - t0:.0f} s'


def units_of(prefix):
    logic = json.load(open(LOGIC.format(prefix)))
    units = []
    for clip in sorted(logic):
        for path in sorted(logic[clip]['paths'], key=lambda p: (p != 'main', p)):
            units.append((clip, path, logic[clip]['paths'][path]))
    return logic, units


# where the game goes after the intro: the 3DO shows its start screen there, and New Game (SELECT.BIN: SELECTNW)
# starts clip 6001; there is no start screen yet, so the intro goes straight on
AFTER = {'BD01_003': 'BD06_001'}


def is_death_file(clip, L):
    """Death scenes stored as clips of their own: the random deaths of a wrong way (BD20_500 ... BD20_580) and others
    (BD02_007): a clip without decisions or a jump that ends in its revive clip."""
    return bool(L.get('revive')) and not L['jump'] and not [d for d in L['decisions'] if d['path'] == 'main']


def resolve(logic, units):
    """Unit table with the logic in unit numbers.  Kinds: main, death (a death path of a clip), deathfile (a death
    clip of its own: after it, its revive clip, then back to the clip where the way was chosen), revive (a clip
    some clip names as its revive clip).  Route outcomes are units or {'list': [units]} (one at random);
    conds: COND rules ({'target', 'then', 'visited'} in units)."""
    num = {f'{c}_{p}': k for k, (c, p, _) in enumerate(units)}
    main = lambda clip: num.get(f'{clip}_main')
    revives = {main(L['revive']) for L in logic.values() if L.get('revive')}
    def out_of(v):                                  # (clips not in this set: not offered)
        if isinstance(v, list):
            v = [main(x) for x in v if main(x) is not None]
            return {'list': v} if v else None
        return None if v is None else main(v)
    table = []
    for k, (clip, path, idx) in enumerate(units):
        L = logic[clip]
        kind = 'death' if path != 'main' else 'revive' if k in revives else 'deathfile' if is_death_file(clip, L) else 'main'
        u = {'unit': k, 'name': f'{clip}_{path}', 'frames': len(idx), 'decisions': [], 'next': None, 'kind': kind}
        if path == 'main':
            for d in L['decisions']:
                if d['path'] != 'main':
                    continue
                if d['kind'] == 'death':
                    out = [None if v is None else num[f'{clip}_{v}'] for v in d['outcomes'].values()]
                else:
                    out = [out_of(v) for v in d['outcomes'].values()]
                u['decisions'].append({'frame': d['frame'], 'window': d['window'], 'kind': d['kind'], 'outcomes': out,
                                       'cut': d.get('cut', 0)})
            u['conds'] = [{'target': main(c['target']), 'then': main(c['then']) if c['then'] else None,
                           'visited': [main(v) for v in c['visited']]} for c in L.get('cond', [])
                          if main(c['target']) is not None and all(main(v) is not None for v in c['visited'])]
            u['next'] = main(L['jump']) if L['jump'] else main(AFTER[clip]) if clip in AFTER else None
            if u['next'] is None and L['revive'] and kind in ('main', 'deathfile'):
                u['next'] = main(L['revive'])           # the revive clip after it ...
                if kind == 'main':
                    u['restart'] = k                    # ... no route chosen: the clip ended in a death for waiting
        else:                                           # a death scene: then the revive clip, then this clip again
            u['next'] = main(L['revive'])
            u['restart'] = main(clip)
        table.append(u)
    return table


if __name__ == '__main__':
    prefix = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4      # ~1 MB of cel data per frame in each worker
    os.makedirs(CACHE, exist_ok=True)
    logic, units = units_of(prefix)
    print(f'{len(units)} units; {workers} workers', flush=True)
    t0 = time.time()
    with Pool(workers) as p:
        for name, msg in p.imap_unordered(encode_unit, sorted(units, key=lambda u: -len(u[2]))):
            print(f'  {name}: {msg} ({time.time() - t0:.0f} s)', flush=True)
    table = resolve(logic, units)
    for u in table:
        d = pickle.load(open(os.path.join(CACHE, u['name'] + '.pkl'), 'rb'))
        u['file'] = os.path.join(CACHE, u['name'] + '.bin'); u['bytes'] = d['bytes']; u['scenes'] = d['scenes']
    out = os.path.join(HERE, 'out', f'set_{prefix}.json')
    json.dump(table, open(out, 'w'), indent=1)
    tot = sum(u['bytes'] for u in table); fr = sum(u['frames'] for u in table)
    print(f'{out}: {len(table)} units, {fr} frames, {tot / 2 ** 20:.1f} MB, {tot / (fr / 12) / 1024:.0f} KB/s average')

"""Turn a cheat map into a test script for build_disc.py:
  python cheat_script.py out/set_ALL.json ALLCHEAT [PAUSE=unit:frame]  -> prints SCRIPT=..., START=... and the
                                                                         expected unit order

The 3DO disc has the game's own test runs (<stage>TEST/DEATH/CHEAT.BIN: "full test", "deaths", "no deaths" for
GDN VIV MOO BLU RED HAL FTZ BRN and ALL; the DOS CD has the same as X1-X27.BIN, which also work here): the first
clip number, then the presses in order, U D L R F.  Each press goes to the next decision met (a press a route does
not offer is ignored and the next one goes to the same decision).  COND rules are followed (visited clips); a random
list (a wrong way's death) is shown as its first clip - any of them comes back to the same clip.  PAUSE adds a START
at that unit frame (before its first decision) the first time the unit plays; unpausing restarts the unit."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHEATS = [os.path.join(HERE, '..', 'bd13', '3do', 'extracted', 'cheat'), os.path.join(HERE, '..', 'bd13', 'dos', 'cheat')]
SLOT = {'U': 1, 'D': 2, 'L': 3, 'R': 4, 'F': 5}
BUTTON = {'U': 'up', 'D': 'down', 'L': 'left', 'R': 'right', 'F': 'fire'}


def cheat(name):
    for d in CHEATS:
        p = os.path.join(d, name.upper() + '.BIN')
        if os.path.exists(p):
            lines = open(p, 'rb').read().decode('latin1').replace('\x1a', '').split()
            return int(lines[0]), ''.join(l for l in lines[1:] if l != 'END')
    sys.exit(f'no cheat map {name}')


def run(units, first, keys, pause=None):
    by = {u['name']: u for u in units}
    u = by[f'BD{first // 1000:02d}_{first % 1000:03d}_main']
    start = u['unit']
    script, order, shown, visited = [], [], 0, set()
    k, paused = 0, False

    def route(o, unit):
        if isinstance(o, dict):
            o = o['list'][0]
        for c in unit.get('conds') or []:
            if o == c['target'] and all(v in visited for v in c['visited']):
                o = c['then']
        return o
    plays = {}
    while True:
        order.append(u['name']); visited.add(u['unit'])
        plays[u['unit']] = plays.get(u['unit'], 0) + 1
        if plays[u['unit']] > 40:                           # a loop no press gets out of
            order.append(f'(stuck: {u["name"]} keeps repeating, {len(keys) - k} presses left)')
            return start, script, order, shown
        if pause and pause[0] == u['unit'] and not paused:
            assert not u['decisions'] or pause[1] < u['decisions'][0]['frame'], 'pause before the first decision'
            script.append((u['unit'], pause[1], 'start')); paused = True
            shown += pause[1] + 1
            order.append(u['name'] + ' (paused, restarted)')
        nxt, again, after = None, None, u['frames']
        for d in u['decisions']:
            f = d['frame'] + 2                              # press 2 frames into the window
            while True:
                if k >= len(keys):
                    return start, script, order, shown + d['frame']
                s = SLOT[keys[k]]
                o = d['outcomes'][s] if d['kind'] == 'death' else route(d['outcomes'][s], u)
                script.append((u['unit'], f, BUTTON[keys[k]])); k += 1
                if d['kind'] == 'death':
                    if o is not None:                       # wrong: the death scene, the revive clip, this clip
                        nxt, again, after = o, u['unit'], f + 1
                    break
                if o is not None:                           # route chosen: it takes over at the switch frame
                    nxt, after = o, d['cut']
                    if units[o]['kind'] == 'deathfile':     # a death of its own: back here after its revive clip
                        again = u['unit']
                    break
                f += 2                                      # not offered: ignored
            if nxt is not None:
                break
        shown += after
        if nxt is None:
            nxt = u['next']
            if u.get('restart') is not None:
                again = u['restart']
            if nxt is None:
                return start, script, order, shown
        if again is not None:                               # death, revive clip, then the clip again
            d = units[nxt]; order.append(d['name']); shown += d['frames']
            if d['kind'] != 'revive':                       # (a death for waiting: the revive clip comes next)
                r = units[d['next']]; order.append(r['name']); shown += r['frames']
            nxt = again
        u = units[nxt]


if __name__ == '__main__':
    units = json.load(open(sys.argv[1]))
    first, keys = cheat(sys.argv[2])
    pause = None
    for a in sys.argv[3:]:
        if a.startswith('PAUSE='):
            pause = tuple(map(int, a[6:].split(':')))
    start, script, order, shown = run(units, first, keys, pause)
    print('first clip', first, '-', len(keys), 'presses')
    print(' -> '.join(order))
    print(f'about {shown / 12:.0f} s of video')
    print(f'START={start}')
    print('SCRIPT=' + ','.join(f'{u}:{f}:{b}' for u, f, b in script))

"""Check the cheat code and menu in the release ROM with the interpreter (sim_player.py), pressing the joypad as a
player would:  python check_cheat.py [MAP ...] [--workers 4]

Each map: B skips the intro; on the start screen Up Down Up Down Up Down Up Down, Left opens the cheat menu; Right
steps to the map's entry (its box must be the one shown); Start plays it, and the units must follow the map's expected
order (build/game/cheat_expected.json, from pack_game.py).  With no maps named, all 27, and the menu checks: Select goes
back to the start screen; a code broken by Right (Up Down x4, Right, Left) does not open the menu, and the right code
after it does; Down Down Up shows the second entry's box.
"""
import json, multiprocessing, os, re, sys
import sim_player as P

HERE = os.path.dirname(os.path.abspath(__file__))
UP, DOWN, LEFT, RIGHT, START, SELECT, B = 0x08, 0x04, 0x02, 0x01, 0x10, 0x20, 0x80
CODE = [UP, DOWN] * 4 + [LEFT]
SETTLE = 20                         # fields a menu shows before the first press
GAP = 6                             # fields between presses (each held for 2)


def names():
    return dict((int(n), s) for n, s in re.findall(r'; (\d+) (BD\S+|TITLE|SAVEMENU|CHEATMENU)',
                                                   open(os.path.join(HERE, 'units.inc')).read()))


class Run:
    """The ROM with a press queue: (unit the press waits for, $4219 bits)."""

    def __init__(self, presses):
        self.cpu = P.CPU(open(os.path.join(P.GAME, 'bd13msu.sfc'), 'rb').read(),
                         open(os.path.join(P.GAME, 'bd13msu.msu'), 'rb', buffering=1 << 20))
        self.names, self.queue, self.played = names(), list(presses), []
        self.since, self.release, self.boxes = 0, None, []

    def unit(self):
        return self.names[self.cpu.tracks[-1] - 1] if self.cpu.tracks else None

    def step_field(self):
        cpu, f = self.cpu, self.cpu.fields
        n = len(cpu.tracks)
        oam = cpu.oam
        while cpu.fields == f:
            cpu.step()
            if len(cpu.tracks) > n:
                n = len(cpu.tracks)
                self.played.append(self.unit())
                self.since = 0
        if cpu.oam != oam and cpu.oam_data:
            self.boxes.append((self.unit(), cpu.oam_data[:512]))
        self.since += 1
        if self.release is not None and cpu.fields >= self.release:
            cpu.pad, self.release = None, None
            self.since = SETTLE - GAP
        elif self.release is None and self.queue and self.queue[0][0] == self.unit() and self.since >= SETTLE:
            cpu.pad = (0, self.queue.pop(0)[1])
            self.release = cpu.fields + 2

    def until(self, test, limit=20000):
        for _ in range(limit):
            if test():
                return True
            self.step_field()
        return False


def boxes():
    return [bytes.fromhex(b['oam'])[:512] for b in json.load(open(os.path.join(P.GAME, '..', 'all', 'cheat_buttons.json')))]


def kinds():
    import pack_game
    return {u['name']: u['kind'] for u in pack_game.load_units()}


def check_map(name):
    exp = json.load(open(os.path.join(P.GAME, 'cheat_expected.json')))[name]
    want = [o for o in exp['order'] if not o.startswith('(')]
    k = exp['entry']
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in CODE] + [('CHEATMENU', RIGHT)] * k + [('CHEATMENU', START)])
    if not r.until(lambda: 'CHEATMENU' in r.played and not r.queue and r.release is None, 40000):
        return name, False, f'menu not reached; played {r.played[-5:]}'
    menu = [b for u, b in r.boxes if u == 'CHEATMENU']
    if not menu or menu[-1] != boxes()[k]:
        return name, False, 'the box shown is not the entry chosen'
    kind = kinds()
    at = len(r.played) - r.played[::-1].index('CHEATMENU')     # (the map's units start after the menu)
    free = ('deathfile', 'revive')                  # (a random list: any of its deaths, then that one's revive)
    while len(r.played) - at < len(want):
        n0 = len(r.played)
        if not r.until(lambda: len(r.played) > n0, 200000):
            return name, False, f'stuck in {r.unit()} after {n0 - at} of {len(want)} units'
        for i in range(n0 - at, min(len(r.played) - at, len(want))):
            got, w = r.played[at + i], want[i]
            if got != w and not (kind.get(got) in free and kind.get(w) in free):
                return name, False, f'unit {i}: {got}, expected {w}'
    done = r.played[at:at + len(want)]
    random = sum(1 for g, w in zip(done, want) if g != w)
    return name, True, f'all {len(want)} units as expected ({random} random deaths or revives), {r.cpu.fields} fields'


def start_map(name):
    """A Run playing the map from the cheat menu, and the expected order of its units."""
    exp = json.load(open(os.path.join(P.GAME, 'cheat_expected.json')))[name]
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in CODE] + [('CHEATMENU', RIGHT)] * exp['entry']
            + [('CHEATMENU', START)])
    r.until(lambda: 'CHEATMENU' in r.played and not r.queue and r.release is None, 40000)
    at = len(r.played) - r.played[::-1].index('CHEATMENU')     # (the map's units start after the menu)
    return r, at, [o for o in exp['order'] if not o.startswith('(')]


def pause_in_map(name, k, presses, more=8, wait=120):
    """Play the map to its unit k, START in it (wait fields in), then presses (unit, bits); the units played from the
    map's start, at least k + 1 + more of them."""
    r, at, want = start_map(name)
    r.until(lambda: len(r.played) - at > k, 400000)
    r.until(lambda: False, wait)
    u = r.unit()
    r.queue = [(u, START)] + [(u if w is None else w, b) for w, b in presses]
    r.until(lambda: not r.queue and r.release is None, 4000)
    n = len(r.played)
    r.until(lambda: len(r.played) >= n + more, 300000)
    return r, r.played[at:], want


def new_game():
    import pack_game
    return pack_game.NEW_GAME


def check_cheat_pause():
    """A pause in a cheat map (the Sega CD rules): START again or the save menu Continue plays the clip again and the
    map goes on (its presses again); in a death scene, its clip again; Exit leaves the map."""
    out = []
    kind = kinds()
    free = ('deathfile', 'revive')
    same = lambda g, w: all(a == b or (kind.get(a) in free and kind.get(b) in free) for a, b in zip(g, w)) and len(g) >= len(w)
    k = 5                                                   # GDNTEST unit 5 (a main clip with a press in it)
    r, got, want = pause_in_map('GDNTEST', k, [(None, START)])
    out.append((f'cheat map: pause, START: {want[k]} again, then the map', same(got, want[:k + 1] + want[k:k + 9])))
    r, got, want = pause_in_map('GDNTEST', k, [(None, B), ('SAVEMENU', B)])
    out.append(('cheat map: pause, save menu Continue: the clip again, then the map',
                same([g for g in got if g != 'SAVEMENU'], want[:k + 1] + want[k:k + 8])))
    k = 1                                                   # GDNTEST unit 1: a death scene of unit 0
    r, got, want = pause_in_map('GDNTEST', k, [(None, START)], wait=30)
    out.append((f'cheat map: pause in a death scene: {want[0]} again, then the map', same(got, want[:2] + want[0:8])))
    r, got, want = pause_in_map('GDNTEST', 5, [(None, B), ('SAVEMENU', DOWN), ('SAVEMENU', B), ('TITLE', START)], 2)
    cpu = r.cpu
    sp = cpu.rd16(0x20)
    out.append(('cheat map: pause, save menu Exit: the start screen, a new game with no cheat presses',
                got[got.index('SAVEMENU'):][:3] == ['SAVEMENU', 'TITLE', new_game()] and cpu.wram[0x8C] == 0
                and cpu.rom[sp:sp + 2] == bytes([255, 255])))
    return out


def check_menu():
    out = []
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in CODE] + [('CHEATMENU', SELECT)])
    ok = r.until(lambda: r.played.count('TITLE') >= 2, 40000)
    out.append(('select goes back to the start screen', ok and r.played[-2:] == ['CHEATMENU', 'TITLE']))
    broken = [UP, DOWN] * 4 + [RIGHT, LEFT]
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in broken])
    r.until(lambda: not r.queue and r.release is None, 40000)
    r.until(lambda: False, 120)
    out.append(('a code broken by Right does not open the menu', 'CHEATMENU' not in r.played))
    r.queue = [('TITLE', c) for c in CODE]
    out.append(('the right code after it does', r.until(lambda: 'CHEATMENU' in r.played, 40000)))
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in CODE] + [('CHEATMENU', c) for c in (DOWN, DOWN, UP)])
    r.until(lambda: 'CHEATMENU' in r.played and not r.queue and r.release is None, 40000)
    menu = [b for u, b in r.boxes if u == 'CHEATMENU']
    bx = boxes()
    out.append(('Down Down Up: boxes 0, 1, 2, 1', [bx.index(b) if b in bx else None for b in menu] == [0, 1, 2, 1]))
    r = Run([('BD01_001_main', B)] + [('TITLE', c) for c in CODE] + [('CHEATMENU', c) for c in (UP, LEFT)])
    r.until(lambda: 'CHEATMENU' in r.played and not r.queue and r.release is None, 40000)
    menu = [bx.index(b) if b in bx else None for u, b in r.boxes if u == 'CHEATMENU']
    out.append(('Up and Left on the first entry stay there', menu == [0]))
    return out


def main():
    args = sys.argv[1:]
    workers = int(args[args.index('--workers') + 1]) if '--workers' in args else 4
    maps = [a for a in args if a.isalpha() and a.isupper()] or list(json.load(open(os.path.join(P.GAME, 'cheat_expected.json'))))
    good = True
    if not [a for a in args if a.isalpha() and a.isupper()]:
        for what, ok in check_menu() + check_cheat_pause():
            print(('ok    ' if ok else 'FAIL  ') + what, flush=True)
            good &= ok
    with multiprocessing.Pool(workers) as pool:
        for name, ok, msg in pool.imap_unordered(check_map, maps):
            print(f"{'ok    ' if ok else 'FAIL  '}{name}: {msg}", flush=True)
            good &= ok
    sys.exit(0 if good else 1)


if __name__ == '__main__':
    main()

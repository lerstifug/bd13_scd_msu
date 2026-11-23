"""Pack the whole game for the SNES:  python pack_game.py [START=clip] [CHEAT=name] [--no-msu] [--no-pcm] [--workers 4]

Needs the streams from bulk.py, encode_title.py, encode_menu.py and encode_cheat.py (build/all).  Writes build/game/: bd13msu.msu (every unit, each on a 2 KB boundary),
bd13msu-<unit + 1>.pcm (each unit's audio) and bd13msu.sfc, plus poc_src/units.inc (the game logic in ROM) and
poc_src/cheats.inc (ROM bank 1: the cheat menu's 27 maps as press lists, its boxes, the test presses).
The cheat menu (the 3DO's test menu): Up Down Up Down Up Down Up Down, Left on the start screen.
The logic is the Sega CD player's (segacd/encode_set.resolve): decisions in fields instead of frames.
  START=BD04_001   the game starts there instead of at the intro (BD01_001)
  CHEAT=GDNCHEAT   test presses from one of the game's own cheat maps (they also pick the start clip)
  FROM=BD02_135    with CHEAT: only the map's part from this clip on
  PRESS=TITLE:120:right,BD06_001:200:up   test presses: unit (a clip's main path, or a full unit name), field, move
                   (up down left right fire start select); a pause keeps counting the unit's fields
Prints the units a cheat map should play, and writes them to build/game/expected.json; every map's to
build/game/cheat_expected.json.
"""
import json, multiprocessing, os, struct, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = os.path.join(HERE, '..', 'build', 'all')
OUT = os.path.join(HERE, '..', 'build', 'game')
sys.path.insert(0, os.path.join(HERE, '..', '..', 'segacd'))
import cheat_script, encode_set
import build as B

NAME = 'bd13msu'
NONE = 0xFFFF
F_REVIVE, F_MENU, F_DEATHFILE, F_SKIP, F_TITLE, F_CHEAT = 1, 2, 4, 16, 32, 64   # (the Sega CD's bits; F_CHEAT new)
SILENT = ('title', 'menu', 'cheat')                 # units with no audio track
# the intro plays at boot, then the start screen; any button skips it and the trailers, which also lead there
SKIP = ('BD01_001_main', 'BD01_002_main', 'BD01_003_main', 'BD96_001_main', 'BD51_001_main', 'BD53_001_main')
TO_TITLE = ('BD01_003_main', 'BD96_001_main', 'BD51_001_main', 'BD53_001_main')
NEW_GAME = 'BD06_001_main'
TITLE_ACTIONS = {'new': 0, 'continue': 1, 'demo': 2}
BIT = {'up': 1, 'down': 2, 'left': 4, 'right': 8, 'fire': 16, 'start': 32, 'select': 64}


def load_units():
    logic, units = encode_set.units_of('ALL')
    table = encode_set.resolve(logic, units)
    title = len(table)
    for u in table:
        if u['name'] in TO_TITLE or (u['kind'] == 'main' and u['next'] is None):
            u['next'] = title                           # (the end of the game too)
    table.append(dict(unit=title, name='TITLE', frames=1, decisions=[], next=None, kind='title'))
    table.append(dict(unit=title + 1, name='SAVEMENU', frames=1, decisions=[], next=None, kind='menu'))
    table.append(dict(unit=title + 2, name='CHEATMENU', frames=1, decisions=[], next=None, kind='cheat'))
    for u in table:
        r = json.load(open(os.path.join(ALL, u['name'] + '.json')))
        if not r.get('ok'):
            sys.exit(f"{u['name']}: not encoded ({r.get('error') or 'differs'})")
        at, top = [], 0                                 # the field each frame shows from (a dropped frame: the one before)
        for f in r['shown_at']:
            top = max(top, f)
            at.append(top)
        u['fields'] = at
        assert len(at) == u['frames'], u['name']
    return table


def pack_msu(table):
    offsets, pos = [], 0
    with open(os.path.join(OUT, NAME + '.msu'), 'wb') as out:
        for u in table:
            data = open(os.path.join(ALL, u['name'] + '.msu'), 'rb').read()
            pad = -pos % 2048
            out.write(bytes(pad))
            pos += pad
            offsets.append(pos)
            out.write(data)
            pos += len(data)
    print(f'{NAME}.msu: {len(table)} units, {pos / 2 ** 20:.0f} MB')
    return offsets


def one_pcm(job):
    import encode_snes as S
    name, out = job
    clip, path = name[:8], name[9:]
    S.write_pcm(clip, path, 0, out)
    return os.path.getsize(out)


def pack_pcm(table, workers):
    jobs = [(u['name'], os.path.join(OUT, f"{NAME}-{u['unit'] + 1}.pcm")) for u in table if u['kind'] not in SILENT]
    todo = [j for j in jobs if not os.path.exists(j[1])]
    if todo:
        with multiprocessing.Pool(workers, maxtasksperchild=8) as pool:
            for k, _ in enumerate(pool.imap_unordered(one_pcm, todo), 1):
                if k % 50 == 0 or k == len(todo):
                    print(f'  audio {k}/{len(todo)}', flush=True)
    print(f'audio: {len(jobs)} tracks, {sum(os.path.getsize(j[1]) for j in jobs) / 2 ** 20:.0f} MB')


def neighbours(centres):
    """For each button and direction (up, down, left, right): the nearest button that way, or None (as on the Sega CD:
    within about 63 degrees of the direction, sideways distance counting three times)."""
    out = []
    for i, (x, y) in enumerate(centres):
        row = []
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            best = None
            for j, (u, v) in enumerate(centres):
                along, across = (u - x) * dx + (v - y) * dy, abs((u - x) * dy - (v - y) * dx)
                if j != i and along > 0 and across <= 2 * along:
                    score = along + 3 * across
                    if best is None or score < best[0]:
                        best = (score, j)
            row.append(None if best is None else best[1])
        out.append(row)
    return out


def title_buttons(names):
    """TitleButtons (player.asm B_*) and each button's OAM image, from encode_title.py."""
    buttons = json.load(open(os.path.join(ALL, 'title_buttons.json')))
    near = neighbours([b['centre'] for b in buttons])
    out = ['TitleButtons:']
    for k, (b, nb) in enumerate(zip(buttons, near)):
        unit = names[(b['clip'] + '_main') if b.get('clip') else NEW_GAME]     # (Continue with no save: a new game)
        out.append(f"  .dw {TITLE_ACTIONS[b['action']]}, {unit}, " + ', '.join(str(NONE if v is None else v) for v in nb)
                   + f", TitleOam{k}, 0   ; button {b['action']} {b.get('clip')}")
    for k, b in enumerate(buttons):
        oam = bytes.fromhex(b['oam'])
        out.append(f'TitleOam{k}:')
        out += ['  .db ' + ', '.join(str(v) for v in oam[i:i + 32]) for i in range(0, len(oam), 32)]
    return out


def pause_menu():
    """MenuButtons (as TitleButtons: action, 0, up, down, left, right, OAM image; one above the other) and the PAUSE
    text's data, from encode_menu.py."""
    m = json.load(open(os.path.join(ALL, 'menu_data.json')))
    db = lambda data: ['  .db ' + ', '.join(str(v) for v in data[i:i + 32]) for i in range(0, len(data), 32)]
    n = len(m['buttons'])
    out = [f".DEFINE MENU_FIRST {next(k for k, b in enumerate(m['buttons']) if b['action'] == 0)}", 'MenuButtons:']
    for k, b in enumerate(m['buttons']):
        out.append(f"  .dw {b['action']}, 0, {k - 1 if k else NONE}, {k + 1 if k + 1 < n else NONE}, {NONE}, {NONE}, MenuOam{k}, 0")
    for k, b in enumerate(m['buttons']):
        out += [f'MenuOam{k}:'] + db(bytes.fromhex(b['oam']))
    p = m['pause']
    out += [f".DEFINE PAUSE_TILES {len(p['tiles']) // 64}", f".DEFINE PAUSE_SPRITES {p['sprites']}", 'PauseCgram:',
            '  .dw ' + ', '.join(map(str, p['colours'])), 'PauseTileData:'] + db(bytes.fromhex(p['tiles']))
    out += ['PauseOam:'] + db(bytes.fromhex(p['oam']))
    return out


def map_presses(table, name, start_from=None):
    """A cheat map as test presses: (start unit, [(unit, field, move bits)], the units it should play, video seconds).
    start_from: only the map's part from this clip on."""
    names = {u['name']: u['unit'] for u in table}
    first, keys = cheat_script.cheat(name)
    start, script, order, shown = cheat_script.run(table, first, keys)
    if start_from:
        start = names[start_from + '_main']
        script = script[next(k for k, s in enumerate(script) if s[0] == start):]
        frames = [table[names[o]]['frames'] for o in order]
        at = order.index(start_from + '_main')
        shown, order = shown * sum(frames[at:]) / sum(frames), order[at:]
    presses = []
    for u, f, button in script:
        at = table[u]['fields']
        presses.append((u, at[min(f, len(at) - 1)] + 1, BIT[button]))
    return start, presses, order, shown / 12


def press_list(label, presses):
    return [f'{label}:'] + [f'  .dw {u}, {f}\n  .db {m}' for u, f, m in presses] + ['  .dw $FFFF']


def cheat_menu(table):
    """(CheatButtons for units.inc: as TitleButtons, action 3, the map's start unit, the entry before (up, left) and
    after (down, right) in the 3DO's order, its OAM image, its press list; cheats.inc lines for ROM bank 1: the OAM
    images and press lists; every map's expected units)"""
    buttons = json.load(open(os.path.join(ALL, 'cheat_buttons.json')))
    n = len(buttons)
    head, bank1, expect = ['CheatButtons:'], [], {}
    for k, b in enumerate(buttons):
        start, presses, order, secs = map_presses(table, b['map'])
        prev, nxt = (k - 1 if k else NONE), (k + 1 if k + 1 < n else NONE)
        head.append(f"  .dw 3, {start}, {prev}, {nxt}, {prev}, {nxt}, CheatOam{k}, CheatScript{k}   ; {b['map']}")
        oam = bytes.fromhex(b['oam'])
        bank1 += [f'CheatOam{k}:'] + ['  .db ' + ', '.join(str(v) for v in oam[i:i + 32]) for i in range(0, len(oam), 32)]
        bank1 += press_list(f'CheatScript{k}', presses)
        expect[b['map']] = dict(entry=k, order=order, secs=secs, presses=len(presses))
    return head, bank1, expect


def write_inc(table, offsets, start, skip_to, presses, names):
    """units.inc: the unit table (player.asm U_*), decisions (D_*), COND rules, lists, start screen, test presses."""
    w = lambda v: NONE if v is None else v
    head = [f'; made by pack_game.py: {len(table)} units', f'.DEFINE START_UNIT {start}', f'.DEFINE SKIP_UNIT {skip_to}',
            'UnitTable:']
    decs, conds, lists = [], [], []
    for u, off in zip(table, offsets):
        k, at, n = u['unit'], u['fields'], u['frames']
        flags = {'revive': F_REVIVE, 'deathfile': F_DEATHFILE, 'title': F_TITLE, 'menu': F_MENU,
                 'cheat': F_CHEAT}.get(u['kind'], 0) \
            | (F_SKIP if u['name'] in SKIP else 0)
        nxt = u['next'] if u['next'] is not None else start     # (the end of the game: from the start)
        rules = u.get('conds') or []
        head.append(f"  .dw ${off & 0xFFFF:04X}, ${off >> 16:04X}, {f'Dec{k}' if u['decisions'] else 0}, {k + 1}, {nxt}, "
                    f"{w(u.get('restart'))}, {f'Cond{k}' if rules else 0}\n  .db {len(u['decisions'])}, {flags}"
                    f"   ; {k} {u['name']}")
        if u['decisions']:
            decs.append(f'Dec{k}:')
        for d in u['decisions']:
            f0, f1 = d['frame'], min(d['frame'] + d['window'], n - 1)
            cut = at[d['cut']] if d['kind'] == 'route' and d['cut'] < n else NONE
            out = []
            for o in d['outcomes']:
                if isinstance(o, dict):
                    out.append(f'List{len(lists)}')
                    lists.append(o['list'])
                else:
                    out.append(str(w(o)))
            decs.append(f"  .dw {at[f0] + 1}, {at[f1] + 1}, {cut}, {0 if d['kind'] == 'death' else 1}, " + ', '.join(out))
        if rules:
            conds.append(f'Cond{k}:\n  .dw {len(rules)}')
            for c in rules:
                conds.append(f"  .dw {c['target']}, {w(c['then'])}, {len(c['visited'])}"
                             + ''.join(f', {v}' for v in c['visited']))
    for i, l in enumerate(lists):
        conds.append(f'List{i}:\n  .dw {len(l)}, ' + ', '.join(map(str, l)))
    # a save belongs to this game logic: another build's unit numbers would mean other clips (test presses and the
    # cheat menu do not count)
    logic = [(u['name'], off, u['next'], u.get('restart')) for u, off in zip(table, offsets) if u['kind'] != 'cheat']
    build_id = zlib.crc32('\n'.join([repr(logic)] + decs + conds).encode()) & 0xFFFF
    head[1:1] = [f".DEFINE MENU_UNIT {names['SAVEMENU']}", f".DEFINE CHEAT_UNIT {names['CHEATMENU']}",
                 f'.DEFINE BUILD_ID {build_id}']
    cheat_head, bank1, expect = cheat_menu(table)
    open(os.path.join(HERE, 'units.inc'), 'w').write(
        '\n'.join(head + decs + conds + title_buttons(names) + pause_menu() + cheat_head) + '\n')
    open(os.path.join(HERE, 'cheats.inc'), 'w').write(
        '\n'.join(['; made by pack_game.py: ROM bank 1 (read with long addressing)'] + press_list('Script', presses)
                   + bank1) + '\n')
    json.dump(expect, open(os.path.join(OUT, 'cheat_expected.json'), 'w'), indent=0)


def main():
    args = sys.argv[1:]
    opts = dict(a.split('=', 1) for a in args if '=' in a)
    workers = int(args[args.index('--workers') + 1]) if '--workers' in args else 4
    os.makedirs(OUT, exist_ok=True)
    table = load_units()
    names = {u['name']: u['unit'] for u in table}
    first = opts.get('START', 'BD01_001')
    start, presses = names['TITLE' if first == 'TITLE' else first + '_main'], []
    if 'CHEAT' in opts:
        start, presses, order, secs = map_presses(table, opts['CHEAT'], opts.get('FROM'))
        print(f"{opts['CHEAT']}: {len(presses)} presses, about {secs:.0f} s of video\n" + ' -> '.join(order))
        json.dump(dict(order=order, secs=secs), open(os.path.join(OUT, 'expected.json'), 'w'), indent=0)
    for e in filter(None, opts.get('PRESS', '').split(',')):
        unit, field, move = e.split(':')
        presses.append((names[unit] if unit in names else names[unit + '_main'], int(field), BIT[move]))
    offsets = pack_msu(table) if '--no-msu' not in args else None
    if offsets is None:
        pos, offsets = 0, []
        for u in table:
            pos += -pos % 2048
            offsets.append(pos)
            pos += os.path.getsize(os.path.join(ALL, u['name'] + '.msu'))
    if '--no-pcm' not in args:
        pack_pcm(table, workers)
    write_inc(table, offsets, start, names['TITLE'], presses, names)
    B.rom(OUT)


if __name__ == '__main__':
    main()

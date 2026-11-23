"""Check pause, the save menu and saved games in the interpreter:  python check_pause.py

Builds test ROMs (pack_game.py; rerun pack_game.py --no-msu --no-pcm for the release ROM) and runs them in sim_player's
65816 interpreter: resuming, the save menu's three buttons, cartridge RAM across a cold boot, Continue with no save or
another build's save, where a pause is not allowed, and where the PAUSE text's tiles land for pictures that leave
little room.  Picture and sound are not checked here.
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, '..', 'build', 'game')
ALL = os.path.join(HERE, '..', 'build', 'all')
sys.path.insert(0, HERE)
import pack_game, sim_player

TABLE = pack_game.load_units()
NAME = {u['unit']: u['name'] for u in TABLE}
NAMES = {u['name']: u for u in TABLE}
PAUSE = json.load(open(os.path.join(ALL, 'menu_data.json')))['pause']
failed = []


def build(start, press):
    subprocess.run([sys.executable, 'pack_game.py', f'START={start}', 'PRESS=' + ','.join(press), '--no-msu', '--no-pcm'],
                   check=True, cwd=HERE, stdout=subprocess.DEVNULL)


def boot(sram=None, bg1map=None):
    rom = open(os.path.join(GAME, 'bd13msu.sfc'), 'rb').read()
    cpu = sim_player.CPU(rom, open(os.path.join(GAME, 'bd13msu.msu'), 'rb', buffering=1 << 20))
    if sram is not None:
        cpu.sram[:] = sram
    if bg1map is not None:
        cpu.bg1map = bg1map
    return cpu


def run(cpu, units, fields=6000):
    stop = cpu.fields + fields
    while len(cpu.tracks) < units and cpu.fields < stop:
        cpu.step()
    return [NAME[t - 1] for t in cpu.tracks]


def check(what, got, want):
    ok = got == want
    print(('ok    ' if ok else 'FAIL  ') + what + ('' if ok else f'\n      got  {got}\n      want {want}'), flush=True)
    if not ok:
        failed.append(what)


def clip_with_room(room):
    """A main clip with no decision for its first `room` fields (and nothing any button skips)"""
    for u in TABLE:
        if u['kind'] == 'main' and u['name'] not in pack_game.SKIP and len(u['fields']) > 1 and u['fields'][-1] > room:
            if u['name'] != pack_game.NEW_GAME and all(u['fields'][d['frame']] > room for d in u['decisions']):
                return u
    sys.exit('no such clip')


def clip_with_death():
    """(clip, death unit, the move that starts it, the field to press it): the clip's first decision is a death one"""
    for u in TABLE:
        if u['kind'] == 'main' and u['decisions'] and u['decisions'][0]['kind'] == 'death':
            d = u['decisions'][0]
            for k, o in enumerate(d['outcomes'][1:]):
                if isinstance(o, int) and TABLE[o]['kind'] != 'deathfile' and TABLE[TABLE[o]['next']]['kind'] == 'revive':
                    return u, TABLE[o], ['up', 'down', 'left', 'right', 'fire'][k], u['fields'][d['frame']] + 3
    sys.exit('no such clip')


def main():
    clip = clip_with_room(900)
    c = clip['name']
    print(f'clip {c}: first decision after field 900')

    # pause, resume; pause, save menu: save, continue; pause, save menu, exit; start screen: Continue
    build(c[:8], [f'{c}:300:start', f'{c}:360:start',                       # pause, START: the clip again
                  f'{c}:200:start', f'{c}:260:fire',                        # pause, fire: the save menu
                  'SAVEMENU:20:up', 'SAVEMENU:30:fire',                     # up to Save, press it
                  'SAVEMENU:90:down', 'SAVEMENU:100:fire',                  # down to Continue, press it
                  f'{c}:200:start', f'{c}:260:fire',
                  'SAVEMENU:20:down', 'SAVEMENU:30:fire',                   # down to Exit, press it
                  'TITLE:30:right', 'TITLE:40:fire'])                       # New Game -> Continue, press it
    cpu = boot()
    got = run(cpu, 9)
    check('pause and resume, save menu, exit, Continue', got[:7], [c, c, 'SAVEMENU', c, 'SAVEMENU', 'TITLE', c])
    unit = clip['unit']
    check('  the save holds BD13 and the clip', (bytes(cpu.sram[:4]), int.from_bytes(cpu.sram[6:8], 'little')), (b'BD13', unit))
    check('  the save holds the visited clip', bool(cpu.sram[8 + (unit >> 3)] & 1 << (unit & 7)), True)
    check('  three pauses with a picture on screen', [p[1] & 1 for p in cpu.pauses if p[3] == unit], [0, 0, 0])
    sram = bytes(cpu.sram)

    # blink: the sprite layer toggles every half second; resume leaves the picture's layers to the stream
    build(c[:8], [f'{c}:200:start', f'{c}:500:start'])
    cpu = boot()
    run(cpu, 2)
    toggles = [f for f, v in cpu.tm_log if f > cpu.pauses[0][0] and f < cpu.pauses[0][0] + 295]
    check('the PAUSE text blinks every 30 fields', sorted({b - a for a, b in zip(toggles, toggles[1:])}), [30])

    # a cold boot: Continue plays the saved clip; with no save or another build's, a new game
    build('TITLE', ['TITLE:30:right', 'TITLE:40:fire'])
    new = pack_game.NEW_GAME
    cpu = boot(sram)
    check('cold boot, Continue: the saved clip', run(cpu, 2), ['TITLE', c])
    check('  the visited clips are back', bool(cpu.wram[0x40 + (unit >> 3)] & 1 << (unit & 7)), True)
    check('cold boot, no save: Continue is a new game', run(boot(), 2), ['TITLE', new])
    other = bytearray(sram)
    other[4] ^= 0xFF
    check('another build\'s save: Continue is a new game', run(boot(other), 2), ['TITLE', new])
    build('TITLE', ['TITLE:40:fire'])
    cpu = boot(sram)
    check('New Game ignores the save', run(cpu, 2), ['TITLE', new])
    check('  and clears the visited clips', bool(cpu.wram[0x40 + (unit >> 3)] & 1 << (unit & 7)), False)

    # a pause in a death scene and in its revive clip: back to the clip
    u, death, move, field = clip_with_death()
    n, d = u['name'], death['name']
    print(f'death: {n} -> {d}')
    build(n[:8], [f'{n}:{field}:{move}', f'{d}:30:start', f'{d}:60:start'])
    check('a pause in a death scene goes back to its clip', run(boot(), 3), [n, d, n])
    rev = TABLE[death['next']]['name']
    build(n[:8], [f'{n}:{field}:{move}', f'{rev}:30:start', f'{rev}:60:start'])
    check('a pause in a revive clip goes back to the clip', run(boot(), 4, 60000), [n, d, rev, n])

    # where a pause is not allowed
    build('BD01_001', ['BD01_001:60:start'])
    cpu = boot()
    check('START in the intro skips it', run(cpu, 2), ['BD01_001_main', 'TITLE'])
    check('  with no pause', [p for p in cpu.pauses if p[3] != NAMES['TITLE']['unit']], [])
    # a press in a blank field waits for the picture
    at = NAMES[c]['fields'][0]
    build(c[:8], [f'{c}:1:start', f'{c}:{at + 60}:start'])
    cpu = boot()
    check('START in a blank field pauses, then resumes', run(cpu, 2), [c, c])
    check('  on a field with a picture', [p[1] & 1 for p in cpu.pauses], [0])
    print(f'      (the first picture comes at field {at}; paused at field {cpu.pauses[0][2]})')

    # where the text goes: maps showing more and more of BG1
    build(c[:8], [f'{c}:200:start'])
    tiles = bytes.fromhex(PAUSE['tiles'])
    names = bytes.fromhex(PAUSE['oam'])[2::4][:PAUSE['sprites']]
    count = len(tiles) // 32
    for label, used, page, table in (('slot 0 only', range(1), 0, 0), ('page 0 full', range(256), 1, 1),
                                     ('pages 0 and 1 full', range(512), 2, 0), ('all but page 3', range(768), 3, 1),
                                     ('all but a few of page 1', [s for s in range(831) if not 300 <= s < 300 + count], 1, 1)):
        m = bytearray(2048)
        for k, s in enumerate(used):
            if k < 1024:
                m[k * 2:k * 2 + 2] = (s | 0x2000 | k % 8 << 10).to_bytes(2, 'little')  # (priority and palette bits too)
        for k in range(len(used), 1024):
            m[k * 2:k * 2 + 2] = (next(iter(used))).to_bytes(2, 'little')
        cpu = boot(None, bytes(m))
        run(cpu, 2, 400)
        o = cpu.oam_data
        good = o is not None and cpu.obj == (2 | page >> 1) and bool(cpu.tm & 0x10 or any(v & 0x10 for _, v in cpu.tm_log))
        base = 0x4000 + page * 0x1000
        placed = set()
        for k in range(PAUSE['sprites']):
            name, attr = o[k * 4 + 2], o[k * 4 + 3]
            good = good and attr & 1 == table and name + page * 256 not in used
            good = good and cpu.tiles.get(base + name * 16) == tiles[names[k] * 32:names[k] * 32 + 32]
            placed.add(name)
        check(f'text with {label}: page {page}, in free slots, the right tiles', (good, len(placed)), (True, count))
    # the alternating buffers show one layer: the text goes in the hidden one's tiles, whatever the map shows
    for label, tm, obj, base in (('BG2 hidden', 1, 0, 0), ('BG1 hidden', 2, 2, 0x4000)):
        cpu = boot(None, bytes(m))
        while cpu.rd16(0x16) < 199 or cpu.rd16(0x12) != unit:
            cpu.step()
        cpu.wram[0x3F] = tm
        run(cpu, 2, 10)
        o = cpu.oam_data
        good = cpu.obj == obj and all(cpu.tiles.get(base + o[k * 4 + 2] * 16) == tiles[names[k] * 32:names[k] * 32 + 32]
                                      and not o[k * 4 + 3] & 1 for k in range(PAUSE['sprites']))
        check(f'text with {label}: in that layer\'s tiles, the right tiles', good, True)
    m = bytearray(2048)
    for k in range(831):
        m[k * 2:k * 2 + 2] = k.to_bytes(2, 'little')
    cpu = boot(None, bytes(m))
    run(cpu, 2, 400)
    check('every slot shown: still paused, no text, no sprite layer',
          (len(cpu.pauses) > 0, not cpu.tiles, any(v & 0x10 for _, v in cpu.tm_log)), (True, True, False))
    print('all good' if not failed else f'{len(failed)} FAILED: {failed}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()

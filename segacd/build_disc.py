"""Build the bootable Brain Dead 13 Sega CD disc (or a test disc).

  python build_disc.py <set.json | stream.bin> [name] [START=unit] [SCRIPT=...] [NAME=value ...]
                                                              -> disc/<name>.iso, .bin, .cue
  (see main().)

Pieces:
  * boot area (sectors 0-15): src/boot.s (volume and disc headers, main-CPU start-up, sub-CPU loader that starts
    APP.BIN), laid out as megadev's lib/cd_boot.s, with the US security code from megadev's lib/security.c.
  * ISO 9660 filesystem from sector 16: APP.BIN (with the test input SCRIPT.BIN built in), MAIN.BIN, STREAMS.DAT
    (every unit's stream, each starting on a sector), BEEPS.BIN and LOGIC.BIN (the game logic with each stream's
    sectors, loaded at boot), TITLE.BIN (the start screen) and CHEATS.BIN (the cheat menu: its 27 maps as press
    lists, its box sprites; loaded at boot).
The .bin is a raw 2352-byte-sector image (sync + header, zero EDC/ECC, which emulators and the MiSTer ignore).
"""
import json, os, sys, struct, subprocess
import beeps

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, '..', 'tools')
def _tool(*cands):
    return next((p for p in cands if os.path.exists(p)), cands[0])


# vasm (http://sun.hasenbraten.de/vasm/, the Motorola syntax m68k build) and megadev (its security code)
VASM = _tool(os.path.join(TOOLS, 'vasm', 'vasmm68k_mot.exe'), os.path.join(TOOLS, 'vasm', 'vasmm68k_mot'))
SECURITY = os.path.join(TOOLS, 'megadev', 'lib', 'security.c')
OUT = os.path.join(HERE, 'disc')
BUILD = os.path.join(HERE, 'build')
SS = 2048


def assemble(src, dst, *defs):
    cmd = [VASM, '-Fbin', '-m68000', '-o', dst] + [f'-D{d}' for d in defs] + [src]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    if r.returncode:
        sys.exit(r.stdout + r.stderr)
    print('assembled', os.path.basename(src), '->', os.path.getsize(dst), 'bytes')


def both32(v):
    return struct.pack('<I', v) + struct.pack('>I', v)


def both16(v):
    return struct.pack('<H', v) + struct.pack('>H', v)


def dir_record(extent, size, flags, name):
    nl = len(name)
    rec = bytearray(33 + nl)
    rec[0] = 0                       # length filled below
    rec[2:10] = both32(extent); rec[10:18] = both32(size)
    rec[18:25] = bytes([100, 1, 1, 0, 0, 0, 0])   # date: 2000-01-01
    rec[25] = flags
    rec[28:32] = both16(1)
    rec[32] = nl; rec[33:33 + nl] = name
    if len(rec) % 2:
        rec += b'\0'
    rec[0] = len(rec)
    return bytes(rec)


VOLUME = b'BRAINDEAD13'                   # the ISO volume ID (src/boot.s has the boot header text)


def security_code(region='US'):
    """The region security code, which the BIOS checks byte for byte at the start of the IP: the byte array for
    REGION == US in lib/security.c of a megadev checkout (tools/megadev, github.com/drojaazu/megadev)."""
    import re
    if not os.path.exists(SECURITY):
        sys.exit(f'{SECURITY} not found: clone github.com/drojaazu/megadev into tools/megadev (see README.md)')
    m = re.search(r'#if REGION == ' + region + r'\b.*?\{(.*?)\};', open(SECURITY).read(), re.S)
    code = bytes(int(x, 16) for x in re.findall(r'0x([0-9A-Fa-f]{2})', m.group(1)))
    assert len(code) == 0x584 and code[:2] == b'\x43\xFA', 'unexpected US security code in ' + SECURITY
    return code


def boot_area():
    """src/boot.s, assembled: the volume and disc headers, the IP (security code + start-up) and the SP (loader)."""
    open(os.path.join(BUILD, 'SECURITY.BIN'), 'wb').write(security_code())
    assemble(os.path.join(HERE, 'src', 'boot.s'), os.path.join(BUILD, 'BOOT.BIN'))
    boot = open(os.path.join(BUILD, 'BOOT.BIN'), 'rb').read()
    assert len(boot) == 16 * SS and boot[:16] == b'SEGADISCSYSTEM  '
    return boot


def build_iso(files):
    """files: list of (name, bytes). Returns the 2048-byte-sector image bytes."""
    boot = boot_area()
    first_file = 21
    layout, lba = [], first_file
    for name, data in files:
        layout.append((name, lba, len(data))); lba += (len(data) + SS - 1) // SS
    total = lba
    root = bytearray()
    root += dir_record(20, SS, 2, b'\0') + dir_record(20, SS, 2, b'\1')
    for name, l, n in layout:
        root += dir_record(l, n, 0, name.encode() + b';1')
    assert len(root) <= SS
    ptl = struct.pack('<BBIH', 1, 0, 20, 1) + b'\0\0'          # root entry, little-endian path table
    ptm = struct.pack('>BBIH', 1, 0, 20, 1) + b'\0\0'
    pvd = bytearray(SS)
    pvd[0] = 1; pvd[1:6] = b'CD001'; pvd[6] = 1
    pvd[8:40] = b'SEGA CD'.ljust(32); pvd[40:72] = VOLUME.ljust(32)
    pvd[80:88] = both32(total); pvd[120:124] = both16(1); pvd[124:128] = both16(1); pvd[128:132] = both16(SS)
    pvd[132:140] = both32(len(ptl))
    pvd[140:144] = struct.pack('<I', 18); pvd[148:152] = struct.pack('>I', 19)
    pvd[156:190] = dir_record(20, SS, 2, b'\0')[:34]
    pvd[190:318] = b' ' * 128; pvd[318:446] = b' ' * 128; pvd[446:574] = b' ' * 128; pvd[574:702] = b' ' * 128
    for off in (813, 830, 847, 864):
        pvd[off:off + 16] = b'0' * 16; pvd[off + 16] = 0
    pvd[881] = 1
    term = bytearray(SS); term[0] = 255; term[1:6] = b'CD001'; term[6] = 1
    img = bytearray(boot)
    img += pvd + term + ptl.ljust(SS, b'\0') + ptm.ljust(SS, b'\0') + bytes(root).ljust(SS, b'\0')
    for (name, data), (_, l, n) in zip(files, layout):
        assert len(img) == l * SS
        img += data.ljust(((len(data) + SS - 1) // SS) * SS, b'\0')
    return bytes(img)


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def to_raw(iso):
    out = bytearray()
    for n in range(len(iso) // SS):
        f = n + 150
        mm, ss, ff = f // 4500, (f // 75) % 60, f % 75
        out += b'\x00' + b'\xff' * 10 + b'\x00' + bytes([bcd(mm), bcd(ss), bcd(ff), 1])
        out += iso[n * SS:(n + 1) * SS] + bytes(8 + 4 + 276)       # EDC/ECC left zero
    return bytes(out)


BUTTONS = {'up': 1, 'down': 2, 'left': 4, 'right': 8, 'fire': 16, 'start': 128,
           'a': 16 | 256, 'b': 16 | 512, 'c': 16 | 1024}      # (A, B, C: the fire bit and their own, payload.s readpad)


def logic_bin(units, lbas, start=0, menu=None):
    """LOGIC.BIN, a disc file the sub CPU loads at boot (app.s): header u16 units, start unit, 0, menu unit; per
    unit (16 bytes) u32 LBA, u16 sectors, u16 next, u16 restart, u16 decisions offset, u16 decisions, u16 flags
    (bit 0 a revive clip: after it, back to the unit saved when the death happened; 1 the save menu: shown, not
    played; 2 a death clip of its own: after its revive clip, back to the clip where the way was chosen; 3 COND
    rules follow the decisions; 4 any button skips it; 5 the start screen; 6 the cheat menu); per decision (20 bytes) u16 frame, window, kind (0 death, 1 route), 6 outcomes
    (no input, up, down, left, right, fire: unit, $FFFF = right move / not offered, or $8000 | word offset of a
    list: u16 count, units - one is picked at random), route switch frame; then, with flag 3, the unit's COND rules:
    u16 rules, per rule u16 target, replacement ($FFFF: no longer offered), u16 count, units that must all have
    been visited.  Lists come last.  $FFFF = none."""
    N = lambda v: 0xFFFF if v is None else v
    off0 = 8 + 16 * len(units)
    body, lists, fix = b'', [], []                  # fix: (position in body, list number)
    table = b''
    for u, (lba, nsec) in zip(units, lbas):
        nxt = u['next']
        if nxt is None and u['kind'] == 'main':
            nxt = start                                 # end of the game (or stage): back to the start
        conds = u.get('conds') or []
        flags = {'revive': 1, 'menu': 2, 'deathfile': 4, 'title': 32, 'cheat': 64}.get(u['kind'], 0) \
            | (8 if conds else 0) \
            | (16 if u.get('skip') else 0)
        table += struct.pack('>IHHHHHH', lba, nsec, N(nxt), N(u.get('restart')), off0 + len(body),
                             len(u['decisions']), flags)
        for d in u['decisions']:
            words = []
            for o in d['outcomes']:
                if isinstance(o, dict):
                    fix.append((len(body) + 6 + 2 * len(words), len(lists))); lists.append(o['list']); words.append(0)
                else:
                    words.append(N(o))
            body += struct.pack('>HHH6HH', d['frame'], d['window'], 0 if d['kind'] == 'death' else 1, *words,
                                d.get('cut', 0))
        if conds:
            body += struct.pack('>H', len(conds))
            for c in conds:
                body += struct.pack('>HHH', c['target'], N(c['then']), len(c['visited']))
                body += b''.join(struct.pack('>H', v) for v in c['visited'])
    body = bytearray(body)
    pos = off0 + len(body)
    starts = []
    for l in lists:
        starts.append(pos)
        body += struct.pack('>H', len(l)) + b''.join(struct.pack('>H', v) for v in l)
        pos += 2 + 2 * len(l)
    for p, k in fix:
        assert starts[k] < 0x10000
        body[p:p + 2] = struct.pack('>H', 0x8000 | (starts[k] >> 1))
    # a save of another build of the disc is ignored (the cheat menu, the last unit, does not count)
    game = table[:16 * sum(1 for u in units if u['kind'] != 'cheat')]
    assert all(u['kind'] != 'cheat' for u in units[:len(game) // 16])
    build = (sum(game) + sum(body)) & 0xFFFF or 1
    head = struct.pack('>HHHH', len(units), start, build, N(menu))
    return head + table + bytes(body)


def script_bin(spec):
    """SCRIPT.BIN: test input, 'unit:frame:button,...' -> u16 unit, frame, buttons, 0 per entry, $FFFF at the end.
    The sub CPU presses the button when it shows that frame of that unit (entries in order, each used once).
    'P:n:button' (unit $FFFE) presses it while paused (pause overlay or save menu), n vertical blanks after the
    previous press."""
    out = b''
    for e in filter(None, spec.split(',')):
        u, f, btn = e.split(':')
        out += struct.pack('>HHHH', 0xFFFE if u == 'P' else int(u), int(f), BUTTONS[btn], 0)
    return out + struct.pack('>HHHH', 0xFFFF, 0, 0, 0)


MENU_STREAM = os.path.join(HERE, 'out', 'set', 'SAVEMENU.bin')
MENU_UNIT = os.path.join(HERE, 'scd', 'MENUS', 'SAVEMENU', 'unit.json')
PAUSE_SPR = os.path.join(HERE, 'out', 'menus', 'PAUSE.SPR')
ACTIONS = {'continue': 0, 'save': 1, 'exit': 2}


def menu_files():
    """build/PAUSE.SPR (the pause overlay, scd_pack.py MENUS; empty if missing) and build/MENU.BIN: u16 buttons,
    per button u16 action (0 continue, 1 save, 2 exit) and CRAM index of its label colour, then the CRAM words of
    a label and of the selected label."""
    import scd_format as F, encode_poc as E
    spr = open(PAUSE_SPR, 'rb').read() if os.path.exists(PAUSE_SPR) else bytes(4)
    open(os.path.join(BUILD, 'PAUSE.SPR'), 'wb').write(spr)
    if not os.path.exists(MENU_UNIT):
        open(os.path.join(BUILD, 'MENU.BIN'), 'wb').write(bytes(6)); return
    m = json.load(open(MENU_UNIT))['menu']
    cw = lambda rgb: E.cram_word(F.rgb_code(rgb))
    data = struct.pack('>H', len(m['items'])) + b''.join(struct.pack('>HH', ACTIONS[i['action']], i['colour'])
                                                         for i in m['items'])
    open(os.path.join(BUILD, 'MENU.BIN'), 'wb').write(data + struct.pack('>HH', cw(m['label']), cw(m['selected'])))


TITLE_STREAM = os.path.join(HERE, 'out', 'set', 'TITLE.bin')
TITLE_SPR = os.path.join(HERE, 'out', 'menus', 'TITLE.SPR')
TITLE_JSON = os.path.join(HERE, 'out', 'menus', 'title.json')
TITLE_ACTIONS = {'new': 0, 'continue': 1, 'demo': 2}


def neighbours(centres):
    """For each button and each direction (up, down, left, right): the nearest button that way (index), or None.
    Buttons within about 63 degrees of the direction; the score weighs sideways distance three times."""
    out = []
    for i, (x, y) in enumerate(centres):
        row = []
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            best = None
            for cone in (2,):
                for j, (u, v) in enumerate(centres):
                    along, across = (u - x) * dx + (v - y) * dy, abs((u - x) * dy - (v - y) * dx)
                    if j != i and along > 0 and across <= cone * along:
                        score = along + 3 * across
                        if best is None or score < best[0]:
                            best = (score, j)
                if best:
                    break
            row.append(None if best is None else best[1])
        out.append(row)
    return out


def title_bin(names, new_game):
    """build/TITLE.BIN, loaded at boot (app.s titlectl): u16 start screen unit ($FFFF: none), New Game's unit,
    buttons; per button (16 bytes) u16 action (0 New Game, 1 Continue, 2 a trailer / the intro), unit, the button
    up / down / left / right ($FFFF: none), offset of its outline sprite block, 0; then the blocks (scd_pack.py
    TITLE.SPR: u16 tiles, u16 sprites, sprites (y, size, tile, x), 4-bit tiles)."""
    N = lambda v: 0xFFFF if v is None else v
    if 'TITLE' not in names or not os.path.exists(TITLE_JSON):
        open(os.path.join(BUILD, 'TITLE.BIN'), 'wb').write(struct.pack('>HHH', 0xFFFF, 0, 0)); return
    info = json.load(open(TITLE_JSON))
    spr = open(TITLE_SPR, 'rb').read()
    nb = struct.unpack('>H', spr[:2])[0]
    offs = struct.unpack(f'>{nb}H', spr[2:2 + 2 * nb])
    head = 6 + 16 * len(info)
    nbr = neighbours([b['centre'] for b in info])
    data = struct.pack('>HHH', names['TITLE'], new_game, len(info))
    for b, o, nb_ in zip(info, offs, nbr):
        unit = names.get(f"{b['clip']}_main", new_game) if b.get('clip') else new_game
        data += struct.pack('>8H', TITLE_ACTIONS[b['action']], unit, *[N(x) for x in nb_], head + o - (2 + 2 * nb), 0)
    open(os.path.join(BUILD, 'TITLE.BIN'), 'wb').write(data + spr[2 + 2 * nb:])


CHEAT_STREAM = os.path.join(HERE, 'out', 'set', 'CHEATMENU.bin')
CHEAT_SPR = os.path.join(HERE, 'out', 'menus', 'CHEAT.SPR')
CHEAT_JSON = os.path.join(HERE, 'out', 'menus', 'cheat.json')


def cheats_bin(src, names):
    """build/CHEATS.BIN, loaded at boot (app.s cheatctl): u16 cheat menu unit ($FFFF: none), 0, entries; per entry
    (16 bytes, as TITLE.BIN) u16 3, the map's first unit, the entry up / down / left / right (the one before / after
    in the 3DO order; $FFFF: none), offset of its box sprite block, offset of its press list; then the blocks
    (scd_pack.py CHEAT.SPR), then the press lists (as SCRIPT.BIN: u16 unit, frame, buttons, 0 ... $FFFF), each the
    game's own cheat map (cheat_script.py) played from its first clip."""
    import cheat_script
    N = lambda v: 0xFFFF if v is None else v
    if 'CHEATMENU' not in names:
        open(os.path.join(BUILD, 'CHEATS.BIN'), 'wb').write(struct.pack('>HHH', 0xFFFF, 0, 0)); return
    info = json.load(open(CHEAT_JSON))
    spr = open(CHEAT_SPR, 'rb').read()
    nb = struct.unpack('>H', spr[:2])[0]
    offs = struct.unpack(f'>{nb}H', spr[2:2 + 2 * nb])
    blocks = spr[2 + 2 * nb:]
    head = 6 + 16 * len(info)
    game = json.load(open(src))                         # (as the set has it: the game's end ends a map's run)
    lists, firsts = b'', []
    for e in info:
        first, keys = cheat_script.cheat(e['map'])
        start, script, order, shown = cheat_script.run(game, first, keys)
        firsts.append((start, len(lists)))
        lists += b''.join(struct.pack('>HHHH', u, f, BUTTONS[b], 0) for u, f, b in script)
        lists += struct.pack('>HHHH', 0xFFFF, 0, 0, 0)
    assert head + len(blocks) + len(lists) < 0x10000
    data = struct.pack('>HHH', names['CHEATMENU'], 0, len(info))
    n = len(info)
    for k, (o, (start, lo)) in enumerate(zip(offs, firsts)):
        prev, nxt = (k - 1 if k else None), (k + 1 if k + 1 < n else None)
        data += struct.pack('>8H', 3, start, N(prev), N(nxt), N(prev), N(nxt), head + o - (2 + 2 * nb),
                            head + len(blocks) + lo)
    data += blocks + lists
    open(os.path.join(BUILD, 'CHEATS.BIN'), 'wb').write(data)
    print(f'CHEATS.BIN: {n} maps, {len(lists) // 8 - n} presses, {len(data)} bytes')


def main():
    """python build_disc.py <set.json | stream.bin> [name] [START=unit] [SCRIPT=unit:frame:button,...] [NAME=value]

    set.json (encode_set.py): one stream file per unit plus the logic table; stream.bin: one stream that loops.
    Other NAME=value arguments are assembler defines (e.g. DMA_FIX=0, DEBUG=1; DBGINPUT=1: the backdrop flashes
    green / red for one frame after an accepted / refused press)."""
    os.makedirs(OUT, exist_ok=True); os.makedirs(BUILD, exist_ok=True)
    src = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else 'test'
    opts = dict(a.split('=', 1) for a in sys.argv[3:])
    start = opts.pop('START', None)
    script = opts.pop('SCRIPT', '')
    if opts.get('DBGINPUT'):                            # the input flash uses the payload's debug backdrop
        opts.setdefault('DEBUG', '1')
    extra = [f'{k}={v}' for k, v in opts.items()]
    beeps.build()                                       # build/BEEPS.BIN (a disc file) + BEEPS.INC (app.s)
    if src.endswith('.json'):
        units = json.load(open(src))
        names = {u['name']: u['unit'] for u in units}
        if start is None:                           # the game starts with the intro when the set has it
            start = names.get('BD01_001_main', 0)
        start = names[start] if start in names else start if start in ('TITLE', 'CHEATMENU') else int(start)
    else:                                               # a single stream: one unit that follows itself
        units = [{'unit': 0, 'name': os.path.basename(src), 'file': src, 'kind': 'main', 'decisions': [], 'next': 0}]
        start = int(start or 0)
    menu = None
    if os.path.exists(MENU_STREAM):                     # the save menu (scd_pack.py MENUS): one more unit
        menu = len(units)
        units = units + [{'unit': menu, 'name': 'SAVEMENU', 'file': MENU_STREAM, 'kind': 'menu', 'decisions': [],
                          'next': None}]
    menu_files()
    names = {u['name']: u['unit'] for u in units}
    if src.endswith('.json') and 'BD01_001_main' in names and os.path.exists(TITLE_STREAM):
        # the start screen (3DO SELECT.BIN): the intro (BD01) plays at boot, then the start screen; the intro and the
        # trailers (BD96, BD51, BD53) skip to it on any button and lead to it; so does the end of the game
        title = len(units)
        units = units + [{'unit': title, 'name': 'TITLE', 'file': TITLE_STREAM, 'kind': 'title', 'decisions': [],
                          'next': None}]
        names['TITLE'] = title
        for u in units:
            if u['name'] in ('BD01_001_main', 'BD01_002_main', 'BD01_003_main', 'BD96_001_main', 'BD51_001_main',
                             'BD53_001_main'):
                u['skip'] = True
            if u['name'] in ('BD01_003_main', 'BD96_001_main', 'BD51_001_main', 'BD53_001_main') or \
                    (u['kind'] == 'main' and u['next'] is None):
                u['next'] = title
    if 'TITLE' in names and os.path.exists(CHEAT_STREAM) and os.path.exists(CHEAT_JSON):
        # the cheat menu (the 3DO test menu: Up Down Up Down Up Down Up Down, Left on the start screen): the last unit
        cheatu = len(units)
        units = units + [{'unit': cheatu, 'name': 'CHEATMENU', 'file': CHEAT_STREAM, 'kind': 'cheat',
                          'decisions': [], 'next': None}]
        names['CHEATMENU'] = cheatu
    if isinstance(start, str):                          # (TITLE, CHEATMENU: units added above)
        start = names[start]
    title_bin(names, names.get('BD06_001_main', start))
    cheats_bin(src, names)
    datas = []
    for u in units:
        d = open(u['file'], 'rb').read()
        datas.append(d.ljust((len(d) + SS - 1) // SS * SS, b'\0'))
    b = lambda n: os.path.join(BUILD, n)
    open(b('SCRIPT.BIN'), 'wb').write(script_bin(script))
    assemble(os.path.join(HERE, 'src', 'payload.s'), b('PAYLOAD.BIN'), *extra)
    assemble(os.path.join(HERE, 'src', 'main_stub.s'), b('MAIN.BIN'))
    sec = lambda n: (n + SS - 1) // SS
    lbas = [(0, sec(len(d))) for d in datas]
    assemble(os.path.join(HERE, 'src', 'app.s'), b('APP.BIN'), *extra)
    lba = 21 + sec(os.path.getsize(b('APP.BIN'))) + sec(os.path.getsize(b('MAIN.BIN')))
    lbas = []
    for d in datas:                          # the streams follow the program; LOGIC.BIN (their sectors) after them
        lbas.append((lba, sec(len(d)))); lba += sec(len(d))
    open(b('LOGIC.BIN'), 'wb').write(logic_bin(units, lbas, start, menu))
    files = [('APP.BIN', open(b('APP.BIN'), 'rb').read()), ('MAIN.BIN', open(b('MAIN.BIN'), 'rb').read())]
    files += [('STREAMS.DAT', b''.join(datas))]   # every unit's stream, sector aligned (LOGIC.BIN has their sectors)
    files += [('BEEPS.BIN', open(b('BEEPS.BIN'), 'rb').read()),       # after the streams: their LBAs stay put
              ('LOGIC.BIN', open(b('LOGIC.BIN'), 'rb').read()),
              ('TITLE.BIN', open(b('TITLE.BIN'), 'rb').read()),
              ('CHEATS.BIN', open(b('CHEATS.BIN'), 'rb').read())]
    iso = build_iso(files)
    open(os.path.join(OUT, name + '.iso'), 'wb').write(iso)
    open(os.path.join(OUT, name + '.bin'), 'wb').write(to_raw(iso))
    open(os.path.join(OUT, name + '.cue'), 'w').write(f'FILE "{name}.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    print(f'{len(units)} units, start unit {start}{", script " + script if script else ""}; '
          f'wrote disc/{name}.iso ({len(iso) // SS} sectors, {len(iso) / 2 ** 20:.1f} MB, '
          f'{len(iso) // SS / 333000:.0%} of a 74-minute CD), {name}.bin, {name}.cue')


if __name__ == '__main__':
    main()

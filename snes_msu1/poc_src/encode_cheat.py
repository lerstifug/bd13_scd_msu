"""Encode the 3DO test menu (the cheat menu) for the SNES:  python encode_cheat.py
-> build/all/CHEATMENU.msu, CHEATMENU.json, cheat_buttons.json, screenshots/cheatmenu_sim.png

On the 3DO, Up Down Up Down Up Down Up Down then Left on the start screen opens it (bd13/cheat_hunt/CHEATS.md): TESTBACK
(FULL TEST / DEATHS / NO DEATHS for nine stages) with a yellow box round the selected entry (SELTEST.BIN).

TESTBACK is only text in one 7x7 font on an 8-pixel advance, yellow headers and red entries, so instead of scaling the
picture (which smears the letters) the menu is rebuilt from the font at 1:1, one letter per 8x8 cell (the 3DO's
spacing is proportional; here each letter has a cell, narrow ones centred), the tiles shared by both colours (palette
0 yellow, palette 1 red).  BG1 rows start on picture lines 8n and BG2's (scroll 0) on 8n + 4, so the rows go 20 lines
apart (the 3DO's 19), alternating BG1 and BG2, and every letter sits in one tile row.  Words are centred in three
84-pixel columns to within 2 pixels: a word starting half a tile in uses half-letter tiles.  The box round the selected
entry is 8x8 sprites as on the start screen, one OAM image per entry for the ROM (pack_game.py), their tiles in the
stream at VRAM word $6000.
"""
import json, os, struct
import numpy as np
from PIL import Image
import encode_snes as S
import encode_title as T

YELLOW = 30 << 5 | 31               # BGR555 of (255, 247, 0), the headers and the box
RED = 31                            # (255, 0, 0), the entries
HEADERS = ['FULL TEST', 'DEATHS', 'NO DEATHS']
STAGES = ['GARDEN', 'VIVI', 'MOOSE', 'BLUE WTCH', 'RED WTCH', 'HALLS', 'FRITZ', 'BRAIN', 'ALL']
HEAD_Y, ROW_Y, ROW_DY = 8, 24, 20   # picture lines of the header and first entry row, and between rows
COL_W = 84                          # three columns across the 252-pixel picture
BOX_W, BOX_ABOVE, BOX_BELOW = 80, 5, 5   # the box: 80 wide, 5 lines above and below the 7-line text (as the 3DO's)


def font():
    """The letters of TESTBACK as 8x8 bitmaps (7x7 glyph, a column and a row of space), each checked to be the same
    wherever it appears, in either colour."""
    a = np.array(Image.open(os.path.join(T.MENUS, 'TESTBACK.png')).convert('RGB')).astype(int)
    on = a.sum(2) > 60
    bands, rows = [], np.where(on.any(1))[0]
    start = prev = rows[0]
    for r in list(rows[1:]) + [None]:
        if r is None or r != prev + 1:
            bands.append((start, prev))
            start = r
        prev = r
    words = []
    for (y0, y1), row in zip(bands, [HEADERS] + [[s] * 3 for s in STAGES]):
        for c, word in enumerate(row):
            ys, xs = np.where(on[y0:y1 + 1, 96 * c:96 * c + 96])
            words.append((word, y0 + ys.min(), 96 * c + xs.min()))
    # the 3DO text is proportional: a letter advances its width + 2 (most are 6 wide, I is 4), a space 8, with the
    # odd kerned pair (DEATHS: the T touches the A).  Letters are learnt from words whose letters are apart (one run
    # of inked columns each), and then every letter of every word must be found exactly, within 2 pixels of where
    # the advance puts it
    seen = {}
    for word, top, x0 in words:
        cols = on[top:top + 7, x0:x0 + 96].any(0)
        runs, x = [], 0
        while x < len(cols):
            if cols[x]:
                s = x
                while x < len(cols) and cols[x]:
                    x += 1
                runs.append((s, x))
            x += 1
        letters = [ch for ch in word if ch != ' ']
        if len(runs) == len(letters):
            for ch, (s, e) in zip(letters, runs):
                seen.setdefault(ch, []).append(on[top:top + 7, x0 + s:x0 + e].tobytes() + bytes([e - s]))
    shape = {}
    for ch, found in seen.items():
        best = max(set(found), key=found.count)
        w = best[-1]
        shape[ch] = np.frombuffer(best[:-1], bool).reshape(7, w)
    for word, top, x0 in words:
        x = x0
        for ch in word:
            if ch == ' ':
                x += 8
                continue
            g = shape[ch]
            x = next((x + d for d in (0, -1, -2, 1, 2)
                      if np.array_equal(on[top:top + 7, x + d:x + d + g.shape[1]], g)), None)
            assert x is not None, f'{ch} of {word} is not the font letter'
            x += g.shape[1] + 2
    glyphs = {' ': np.zeros((8, 8), bool)}
    for ch, g in shape.items():                     # one tile each: a 6-wide letter at the left, narrower ones centred
        cell = np.zeros((8, 8), bool)
        off = max(0, (6 - g.shape[1]) // 2)
        cell[:7, off:off + g.shape[1]] = g
        glyphs[ch] = cell
    return glyphs


def layout():
    """[(text, picture line, x, palette, layer)] for the headers and the entries; the entries in the 3DO's order
    (across each row: test, deaths, cheat)"""
    out = []
    for c, h in enumerate(HEADERS):
        out.append((h, HEAD_Y, 0, 'head', c))
    for r, s in enumerate(STAGES):
        for c in range(3):
            out.append((s, ROW_Y + ROW_DY * r, 1, 'entry', c))
    placed = []
    for text, y, pal, kind, c in out:
        centre = COL_W * c + COL_W / 2 + S.PX0
        x = int(round((centre - (8 * len(text) - 2) / 2) / 4)) * 4     # (a half-tile start: half-letter tiles)
        placed.append(dict(text=text, y=y, x=x, pal=pal, kind=kind, col=c, layer=1 if y % 8 == 0 else 2))
    assert all(p['y'] % 8 in (0, 4) for p in placed)
    return placed


def tiles_and_maps(glyphs, placed):
    """(tile data: tile 0 empty, then the letters (a word starting on a tile) and half letters (a word starting half
    a tile in); BG1 map 32x32; BG2 map 64x32; the picture as the PPU shows it).  The tiles are shared by both colours."""
    slots, data = {bytes(32): 0}, [bytes(32)]
    m1, m2 = np.zeros((32, 32), np.int64), np.zeros((32, 64), np.int64)
    pic = np.zeros((S.H, S.W), np.int64)
    colour = [YELLOW, RED]
    for p in placed:
        n = len(p['text'])
        ink = np.zeros((8, 8 * n + 8), bool)
        off = p['x'] % 8
        for i, ch in enumerate(p['text']):
            ink[:, off + 8 * i:off + 8 * i + 8] = glyphs[ch]
        for k in range(ink.shape[1] // 8):
            cell = ink[:, 8 * k:8 * k + 8]
            if not cell.any():
                continue
            t = S.planar(cell[None].astype(np.int64))[0]
            if t not in slots:
                slots[t] = len(data)
                data.append(t)
            tx = p['x'] // 8 + k
            e = slots[t] | p['pal'] << 10
            if p['layer'] == 1:
                m1[S.ROW0 + p['y'] // 8, tx] = e        # picture line j shows BG1 map line 8 + j
            else:
                m2[(p['y'] + 4) // 8, tx] = e           # BG2, scroll 0: picture line j shows map line 4 + j
            pic[p['y']:p['y'] + 8, tx * 8:tx * 8 + 8][cell] = colour[p['pal']]
    return b''.join(data), m1, m2, S.window(pic)


def boxes(placed):
    """(sprite tiles, per entry: map, centre, sprites, OAM image): a 1-pixel yellow rectangle round each entry"""
    lay = json.load(open(os.path.join(T.MENUS, 'seltest.json')))
    entries = [p for p in placed if p['kind'] == 'entry']
    assert len(entries) == len(lay['entries'])
    slots, tiles, out = {bytes(32): 0}, [bytes(32)], []
    for e, p in zip(lay['entries'], entries):
        scr = np.zeros((224, 256), np.uint8)
        x0 = COL_W * p['col'] + (COL_W - BOX_W) // 2 + S.PX0
        y0, y1 = p['y'] - BOX_ABOVE + 4, p['y'] + 6 + BOX_BELOW + 4      # (screen lines: the picture starts at 4)
        scr[y0, x0:x0 + BOX_W] = scr[y1, x0:x0 + BOX_W] = 1
        scr[y0:y1 + 1, x0] = scr[y0:y1 + 1, x0 + BOX_W - 1] = 1
        oam = bytearray()
        for cy in range(28):
            for cx in range(32):
                cell = scr[cy * 8:cy * 8 + 8, cx * 8:cx * 8 + 8]
                if not cell.any():
                    continue
                t = S.planar(cell[None].astype(np.int64))[0]
                if t not in slots:
                    slots[t] = len(tiles)
                    tiles.append(t)
                oam += bytes([cx * 8, cy * 8 - 1, slots[t], T.ATTR])        # (a sprite shows from the line after its y)
        n = len(oam) // 4
        assert n <= 128, (e['map'], n)
        oam += bytes([0, 240, 0, 0]) * (128 - n) + bytes(32)
        out.append(dict(map=e['map'], centre=[x0 + BOX_W / 2, (y0 + y1) / 2 - 4], sprites=n, oam=oam.hex(),
                        box=[x0, y0 - 4, BOX_W, y1 - y0 + 1]))
    return b''.join(tiles), out


def stream(data, m1, m2, shown, obj):
    """The one-picture unit: font tiles for both layers, both maps, colours, box tiles -> (stream, blank fields)"""
    cg = struct.pack('<4H', 0, YELLOW, 0, 0) + bytes(24) + struct.pack('<4H', 0, RED, 0, 0) + bytes(24)
    body = (S.cmd_reg(0x08, 0x75) + S.cmd_reg(0x2C, 0x13) + S.cmd_cgram(0, cg) + S.cmd_cgram(T.OBJ_CGRAM, S.words([0, YELLOW]))
            + S.cmd_vram(0, data) + S.cmd_vram(S.CEL_BASE * 16, data) + S.cmd_vram(T.OBJ_BASE, obj)
            + S.cmd_vram(S.MAP1, S.words(m1.ravel())) + S.cmd_vram(S.MAP2, S.words(m2[:, :32].ravel()))
            + S.cmd_vram(S.MAP2 + 0x400, S.words(m2[:, 32:].ravel())))
    k = S.size(body) // S.PACKET_BLANK + 2
    packets = [[] for _ in range(k + 1)]
    S.fill(packets, [(f, S.PACKET_BLANK) for f in range(k)], body)
    packets[k] += S.cmd_scroll(0, 0)
    out = bytearray()
    for f, p in enumerate(packets):
        assert len(p) < 256
        out += bytes([1 if f < k else 0, len(p)]) + b''.join(p)
    out += bytes([0x80, 0])
    bad, _ = S.simulate(bytes(out), {k: (0, shown)}, keep=False)
    assert not bad, 'the stream does not show the picture'
    bad, _ = S.simulate(bytes(out), {k: (0, shown)}, keep=False, dirty=True)
    assert not bad, 'the picture depends on what played before'
    return bytes(out), k


def main():
    glyphs = font()
    placed = layout()
    data, m1, m2, shown = tiles_and_maps(glyphs, placed)
    obj, buttons = boxes(placed)
    out, k = stream(data, m1, m2, shown, obj)
    open(os.path.join(T.ALL, 'CHEATMENU.msu'), 'wb').write(out)
    json.dump(dict(unit='CHEATMENU', ok=True, differ=0, shown_at=[k], frames=1, fields=k + 1, kb=len(out) // 1024),
              open(os.path.join(T.ALL, 'CHEATMENU.json'), 'w'))
    json.dump(buttons, open(os.path.join(T.ALL, 'cheat_buttons.json'), 'w'), indent=0)
    img = S.rgb555(shown).astype(np.uint8)                              # with the first entry's box
    x0, y0, w, h = buttons[0]['box']
    img[y0, x0:x0 + w] = img[y0 + h - 1, x0:x0 + w] = img[y0:y0 + h, x0] = img[y0:y0 + h, x0 + w - 1] = (255, 247, 0)
    Image.fromarray(img).save(os.path.join(S.SHOTS, 'cheatmenu_sim.png'))
    print(f'CHEATMENU: {len(out) / 1024:.1f} KB, {k} blank fields, {len(data) // 32 - 1} letter tiles, {len(obj) // 32} '
          f'sprite tiles, {len(buttons)} entries, at most {max(b["sprites"] for b in buttons)} sprites')


if __name__ == '__main__':
    main()

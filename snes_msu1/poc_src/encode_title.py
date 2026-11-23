"""Encode the start screen for the SNES:  python encode_title.py  -> build/all/TITLE.msu, TITLE.json, title_buttons.json

The 3DO's SELECTBG on BG2 (252x216, as every picture here) with no sound, a unit like any other.  The outline the 3DO
draws round the selected button (SELECT??.png, blue and white) becomes 8x8 sprites: their tiles go in the stream (VRAM
word $6000), and each button gets a whole OAM image for the ROM (pack_game.py), DMAed when the selection moves.
"""
import json, os, struct
import numpy as np
from PIL import Image
import encode_snes as S

MENUS = os.path.join(S.SRC3, 'menus')
ALL = os.path.join(S.BUILD, 'all')
OBJ_BASE = 0x6000                   # word address of the sprite tiles ($2101 = $03; the next unit's tiles overwrite them)
OBJ_CGRAM = 128                     # CGRAM entry of sprite palette 0
BLUE, WHITE = 0x7C00, 0x7FFF        # BGR555: the outline's two colours
ATTR = 0x30                         # sprite priority 3, palette 0, name table 0
OAM_SIZE = 544


def background(path=os.path.join(S.SRC3, 'SELECTBG.png'), black=None):
    """(tile data, map entries in rows, palette words, picture as the PPU should show it); black: picture pixels to
    make black (the save menu's labels)"""
    a = np.array(Image.open(path).convert('RGB'))
    lin = S.resize_lin(a, size=(S.PW, S.H))[0]
    if black is not None:
        lin = lin.copy()
        lin[black] = 0
    lin = np.pad(lin, ((0, 0), (0, S.W - S.PW), (0, 0)), mode='edge')
    T = S.lin2lab(lin.reshape(S.TH, 8, S.TW, 8, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 64, 3))
    M = np.ones(T.shape[:2], bool)
    pals, pal_words = S.fit_palettes(T, M, np.ones(len(T)))
    pal, px = S.quantise(T, M, pals, S.DITHER)
    data, slots, entries = [], {}, []
    for t, p in zip(S.planar(px), pal):
        if t not in slots:
            slots[t] = len(data)
            data.append(t)
        entries.append(slots[t] | int(p) << 10)
    img = pal_words[np.repeat(pal, 64), px.ravel() - 1].reshape(S.TH, S.TW, 8, 8).transpose(0, 2, 1, 3).reshape(S.H, S.W)
    shown = np.zeros((S.H, S.W), np.int64)
    shown[:, S.PX0:] = img[:, :S.W - S.PX0]
    return b''.join(data), entries, pal_words, S.window(shown)


def outlines():
    """(the sprite tiles, the buttons: action, clip, centre, OAM image)"""
    lay = json.load(open(os.path.join(MENUS, 'select.json')))
    slots, tiles, buttons = {bytes(32): 0}, [bytes(32)], []
    for b in lay['buttons']:
        a = np.array(Image.open(os.path.join(MENUS, b['outline'])).convert('RGBA'))
        scr = np.zeros((224, 256), np.uint8)
        ys, xs = np.where(a[:, :, 3] > 0)
        white = a[ys, xs, 0] >= 128
        sx = np.minimum(((xs + 0.5) * S.SX).astype(int), S.PW - 1) + S.PX0  # (3DO pixels -> 8:7, as the picture)
        sy = ys + 4
        scr[sy[~white], sx[~white]] = 1
        scr[sy[white], sx[white]] = 2                                       # white over blue where they share a pixel
        oam = bytearray()
        for cy in range(28):
            for cx in range(32):
                cell = scr[cy * 8:cy * 8 + 8, cx * 8:cx * 8 + 8]
                if not cell.any():
                    continue
                t = S.planar(cell[None])[0]
                if t not in slots:
                    slots[t] = len(tiles)
                    tiles.append(t)
                oam += bytes([cx * 8, cy * 8 - 1, slots[t], ATTR])          # (a sprite shows from the line after its y)
        n = len(oam) // 4
        assert n <= 128, (b['outline'], n)
        oam += bytes([0, 240, 0, 0]) * (128 - n) + bytes(32)                # the rest off screen; the high table: zeros
        assert len(oam) == OAM_SIZE
        buttons.append(dict(action=b['action'], clip=b.get('clip'), centre=[float(xs.mean()), float(ys.mean())],
                            sprites=n, oam=oam.hex()))
    assert len(tiles) <= 320, len(tiles)                                    # below BG2's map at $7400
    return b''.join(tiles), buttons


def stream(bg, entries, pal_words, shown, obj, colours):
    """A one-picture unit: the picture on BG2, its sprite tiles and colours -> (stream, blank fields); checked exactly."""
    cg = b''.join(struct.pack('<H', 0) + S.words(pal_words[p]) for p in range(8))
    body = (S.cmd_reg(0x08, 0x75) + S.cmd_reg(0x2C, 0x12) + S.cmd_cgram(0, cg)
            + S.cmd_cgram(OBJ_CGRAM, S.words(colours)) + S.cmd_vram(0, bg) + S.cmd_vram(OBJ_BASE, obj)
            + S.cmd_vram(S.MAP2 + S.ROW0 * 32, S.words(entries)))
    k = S.size(body) // S.PACKET_BLANK + 2
    packets = [[] for _ in range(k + 1)]
    S.fill(packets, [(f, S.PACKET_BLANK) for f in range(k)], body)
    packets[k] += S.cmd_scroll(512 - S.PX0, 8 * S.ROW0 - 4)                 # map column 0 at x 2, row 1 at the picture's top
    data = bytearray()
    for f, p in enumerate(packets):
        assert len(p) < 256
        data += bytes([1 if f < k else 0, len(p)]) + b''.join(p)
    data += bytes([0x80, 0])
    bad, _ = S.simulate(bytes(data), {k: (0, shown)}, keep=False)
    assert not bad, 'the stream does not show the picture'
    bad, _ = S.simulate(bytes(data), {k: (0, shown)}, keep=False, dirty=True)
    assert not bad, 'the picture depends on what played before'
    return bytes(data), k


def main():
    bg, entries, pal_words, shown = background()
    obj, buttons = outlines()
    data, k = stream(bg, entries, pal_words, shown, obj, [0, BLUE, WHITE])
    os.makedirs(ALL, exist_ok=True)
    open(os.path.join(ALL, 'TITLE.msu'), 'wb').write(data)
    json.dump(dict(unit='TITLE', ok=True, differ=0, shown_at=[k], frames=1, fields=k + 1, kb=len(data) // 1024),
              open(os.path.join(ALL, 'TITLE.json'), 'w'))
    json.dump(buttons, open(os.path.join(ALL, 'title_buttons.json'), 'w'), indent=0)
    Image.fromarray(S.rgb555(shown).astype(np.uint8)).save(os.path.join(S.SHOTS, 'title_sim.png'))
    print(f'TITLE: {len(data) / 1024:.0f} KB, {k} blank fields, {len(bg) // 32} tiles, {len(obj) // 32} sprite tiles; sprites '
          + ' '.join(f"{b['action']}:{b['sprites']}" for b in buttons))


if __name__ == '__main__':
    main()

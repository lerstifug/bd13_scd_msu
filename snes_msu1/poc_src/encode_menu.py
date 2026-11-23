"""Encode the save menu and the PAUSE text for the SNES:  python encode_menu.py  -> build/all/SAVEMENU.msu, SAVEMENU.json, menu_data.json

The save menu is the 3DO's SAVEBACK on BG2, a silent one-picture unit like the start screen.  Its three labels are black
in the picture; the selected one is drawn over that in the 3DO's label colour by 8x8 sprites (an OAM image per button,
in ROM, pack_game.py).  The PAUSE text (PAUSE.png, in its own two colours, in the middle of the picture) has no room in
VRAM during play: player.asm finds tile slots for it when paused, so here it is just tiles and an OAM image with the
text's own tile numbers.
"""
import json, os
import numpy as np
from PIL import Image
import encode_snes as S
import encode_title as T

CYAN = 0x7FE0                       # BGR555: the labels' own colour (player.asm LABEL_COLOUR)


def scaled(path):
    """(picture y, picture x, rgba) of the cel's opaque pixels: 3DO pixels -> 8:7, as the picture"""
    a = np.array(Image.open(path).convert('RGBA'))
    ys, xs = np.where(a[:, :, 3] > 0)
    return ys, np.minimum(((xs + 0.5) * S.SX).astype(int), S.PW - 1), a[ys, xs]


def oam_image(scr, slots, tiles):
    """A screen image (224x256, colours 1-15) -> the 8x8 sprites showing it, as a whole OAM image; tiles added"""
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
            oam += bytes([cx * 8, cy * 8 - 1, slots[t], T.ATTR])            # (a sprite shows from the line after its y)
    n = len(oam) // 4
    assert n <= 128, n
    oam += bytes([0, 240, 0, 0]) * (128 - n) + bytes(32)                    # the rest off screen; the high table: zeros
    assert len(oam) == T.OAM_SIZE
    return oam, n


def pause_text():
    ys, xs, px = scaled(os.path.join(T.MENUS, 'PAUSE.png'))
    h, w = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
    y0, x0 = 4 + (S.H - h) // 2, S.PX0 + (S.PW - w) // 2                    # in the middle, as on the Sega CD
    scr = np.zeros((224, 256), np.uint8)
    white = px[:, 0] >= 128
    scr[ys - ys.min() + y0, xs - xs.min() + x0] = np.where(white, 1, 2)
    slots, tiles = {}, []
    oam, n = oam_image(scr, slots, tiles)
    rgb = [tuple(px[white][0][:3]), tuple(px[~white][0][:3])]
    colours = [0] + [int(r) >> 3 | (int(g) >> 3) << 5 | (int(b) >> 3) << 10 for r, g, b in rgb]
    return dict(tiles=b''.join(tiles).hex(), oam=oam.hex(), sprites=n, colours=colours), len(tiles)


def main():
    lay = json.load(open(os.path.join(T.MENUS, 'savegame.json')))
    black = np.zeros((S.H, S.PW), bool)
    slots, tiles, buttons = {bytes(32): 0}, [bytes(32)], []
    for b in lay['buttons']:
        ys, xs, _ = scaled(os.path.join(T.MENUS, b['label']))
        black[ys, xs] = True
        scr = np.zeros((224, 256), np.uint8)
        scr[ys + 4, xs + S.PX0] = 1
        oam, n = oam_image(scr, slots, tiles)
        buttons.append(dict(action=b['action'], oam=oam.hex(), sprites=n))
    assert len(tiles) <= 320, len(tiles)
    bg, entries, pal_words, shown = T.background(os.path.join(T.MENUS, lay['background']), black)
    obj = b''.join(tiles)
    data, k = T.stream(bg, entries, pal_words, shown, obj, [0, CYAN])
    open(os.path.join(T.ALL, 'SAVEMENU.msu'), 'wb').write(data)
    json.dump(dict(unit='SAVEMENU', ok=True, differ=0, shown_at=[k], frames=1, fields=k + 1, kb=len(data) // 1024),
              open(os.path.join(T.ALL, 'SAVEMENU.json'), 'w'))
    pause, n = pause_text()
    json.dump(dict(buttons=buttons, pause=pause), open(os.path.join(T.ALL, 'menu_data.json'), 'w'), indent=0)
    Image.fromarray(S.rgb555(shown).astype(np.uint8)).save(os.path.join(S.SHOTS, 'savemenu_sim.png'))
    print(f'SAVEMENU: {len(data) / 1024:.0f} KB, {k} blank fields, {len(bg) // 32} tiles, {len(tiles)} sprite tiles; sprites '
          + ' '.join(str(b['sprites']) for b in buttons) + f"; PAUSE: {n} tiles, {pause['sprites']} sprites")


if __name__ == '__main__':
    main()

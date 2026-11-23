"""The Sega CD bitmap format shared by step 2 (scd_bitmaps.py, writes it) and step 3 (scd_pack.py, reads it).

Folder per unit (a clip path):  scd/<stage>/<unit>/
  unit.json          frames, scenes, audio file (see below)
  sNN_bg.bmp         each scene's background: what the scene's base plane maps show (no trailing number, so it
                     is never taken for part of an animation)
  sNN_cel_FFFF.bmp   every frame's cel layer, FFFF = the frame's number in the unit (decision frames use the same
                     numbers): the pixels where the frame differs from its scene's background (the cel, plus any
                     background pixels in the cel's 8x8 cells that had to change with it); index 0 = transparent
                     (shows the background).  Frame = background with the cel layer on top.  The number is last,
                     so Aseprite (open the first file: "load as animation") and ffmpeg (-start_number FFFF -i
                     sNN_cel_%04d.bmp) see each scene's cel layers as one animation with one palette.
  audio.wav          the soundtrack exactly as played: 8-bit unsigned mono, 14,400 Hz, 1200 samples per frame

Bitmaps are 8-bit indexed BMP files (open them in Aseprite, GIMP, Photoshop...).
  Static backgrounds and cel layers are the full H32 screen, 256x224; the picture is the 224x192 window at x 16,
  y 16, the rest stays index 0.
  The Genesis H32 pixel is wider than tall (about 8:7); the files say so in their resolution fields
  (70 x 80 pixels per inch) and Aseprite's pixel ratio setting can show it.
  A scrolling scene's background is the whole tall virtual background, 256 wide (picture at x 16..239), virtual
  line v on row v; unit.json gives each frame's camera line (the window's top row in it).

Palette: the scene's four palette lines, normal and shadowed, so each pixel's index says everything:
     index = shadow * 64 + line * 16 + colour        (shadow 0/1, line 0-3, colour 1-15; colour 0 = black)
  0        transparent, shown as magenta (255, 0, 255; also Aseprite's default transparent index); black is
           written as 16 (normal) / 80 (shadowed), and colour 0 of any line is the same black.  A cel layer may
           also be saved as 24-bit BMP: magenta pixels are transparent, others map to the scene palette
  0-63     the colours shown normally (= CRAM: each is one of the Genesis' 512 colours)
  64-127   the same colours as shown in a shadowed cell (Shadow/Highlight mode: about half brightness)
  128-255  unused (grey: pixels with these indices are errors)
  Every bitmap of a scene carries the same palette; step 3 takes it from the scene's background bitmap.
  Colour edits: change entries 1-15 of lines 0-3 (indices 1-63); step 3 snaps each to the nearest of the 512
  Genesis colours and derives the shadowed half itself.

Hardware rules for each 8x8 cell (step 2 keeps them; step 3 repairs and reports pixels that break them):
  - all pixels of a cell are normal, or all shadowed (one shadow bit per cell);
  - a cell uses at most two palette lines (plane B underneath, plane A on top); colour 0 (black) fits any line.

Menu screens (scene kind "wide"): the start screen and the save menu run in H40 like the 3DO: 320x224 bitmaps with
  the 3DO's 288x216 picture 1:1 at x 16, y 4; one frame, no cel changes.  Their palette line 3 holds colours the
  player changes or uses for sprites (save menu labels, start screen selection outlines).

unit.json:
  {"unit": "BD04_002_main", "clip": "BD04_002", "path": "main", "frames": 453, "audio": "audio.wav",
   "scenes": [{"kind": "static", "first": 0, "frames": 149, "background": "s00_bg.bmp",
               "cels": "s00_cel_%04d.bmp", "pool": 410},
              {"kind": "scroll", "first": 96, "frames": 178, "background": "s02_bg.bmp",
               "cels": "s02_cel_%04d.bmp", "camera": [...]}]}
  cels: the scene's cel layer file names (% the unit frame number); pool: frame tile pool size (410, or 560 for
  scenes whose cels cover most of the screen).
"""
import json, os, wave
import numpy as np
from PIL import Image
import encode_poc as E

SCREEN_W, SCREEN_H = 256, 224
WX, WY = 16, 16                                     # the 224x192 window inside the H32 screen
DPI = (70, 80)                                      # H32 pixel aspect hint (wide pixels)
WIDE_W, WIDE_H = 320, 224                           # menu screens run in H40, the 3DO's own width
WIDE_X, WIDE_Y = 16, 4                              # where a 3DO 288x216 picture sits on it, 1:1
WIDE_DPI = (72, 72)
TRANSPARENT = (255, 0, 255)                         # index 0: magenta
UNUSED = (128, 128, 128)                            # indices 128-255 (not a Genesis colour: easy to spot)


def cel_pattern(k):
    """File name pattern of scene k's cel layers (% unit frame number)."""
    return f's{k:02d}_cel_%04d.bmp'


def cel_file(sc, f):
    """The cel layer file of unit frame f in scene sc (a unit.json scene)."""
    return sc['cels'] % f


def code_rgb(code, shadow=0):
    return tuple(int(v) for v in (E.GS if shadow else E.GN)[int(code)])


def palette(codes):
    """256 RGB entries for a scene's four lines of grid codes."""
    pal = [UNUSED] * 256
    for s in (0, 1):
        for l in range(4):
            pal[s * 64 + l * 16] = (0, 0, 0)
            for c in range(1, 16):
                pal[s * 64 + l * 16 + c] = code_rgb(codes[l][c - 1], s) if c - 1 < len(codes[l]) else (0, 0, 0)
    pal[0] = TRANSPARENT
    return pal


def rgb_code(rgb):
    """Nearest Genesis grid code of an RGB colour (normal levels)."""
    lv = lambda v: int(np.abs(E.NORM - v).argmin())
    r, g, b = rgb
    return lv(r) * 64 + lv(g) * 8 + lv(b)


def codes_of(pal):
    """Four lines of 15 grid codes from a bitmap palette (its normal half)."""
    return [np.array([rgb_code(pal[l * 16 + c]) for c in range(1, 16)]) for l in range(4)]


def write_bmp(path, idx, pal, wide=False):
    im = Image.fromarray(np.asarray(idx, np.uint8), 'P')
    im.putpalette([v for c in pal for v in c])
    im.save(path, dpi=WIDE_DPI if wide else DPI)


def read_bmp(path, pal=None):
    """(indices (h,w) uint8, palette [(r,g,b)] * 256).  A true-colour bitmap (e.g. a cel layer saved as 24-bit)
    is read against pal (its scene's palette): magenta = index 0 (transparent), other colours the palette entry
    with exactly that colour, else the nearest one."""
    im = Image.open(path)
    if im.mode != 'P':
        if pal is None:
            raise ValueError(f'{path}: not an indexed (8-bit palette) bitmap ({im.mode})')
        rgb = np.array(im.convert('RGB')).astype(int)
        P = np.array(pal[:128], int)
        P[0] = TRANSPARENT
        flat = rgb.reshape(-1, 3)
        keys, inv = np.unique(flat, axis=0, return_inverse=True)
        idx = np.array([16 if not k.any() else int(((P - k) ** 2).sum(1).argmin()) for k in keys], np.uint8)
        return idx[inv.reshape(-1)].reshape(rgb.shape[:2]), list(pal)
    p = im.getpalette() or []
    p = p + [0] * (768 - len(p))
    return np.array(im), [tuple(p[i * 3:i * 3 + 3]) for i in range(256)]


def black16(idx):
    """Black as index 16 / 80, so index 0 stays free for transparency."""
    idx = np.asarray(idx)
    return np.where(idx % 16 == 0, (idx & 64) + 16, idx).astype(np.uint8)


def canon(idx):
    """Colour 0 of any line is the same black: one index per mode (0 / 64)."""
    idx = np.asarray(idx)
    return np.where(idx % 16 == 0, idx & 64, idx)


def cel_layer(frame, bg):
    """The pixels of frame that differ from bg (both indices), black as 16/80, the rest 0 (transparent)."""
    return np.where(canon(frame) != canon(bg), black16(frame), 0).astype(np.uint8)


def compose(bg, cel):
    return np.where(cel != 0, cel, bg).astype(np.uint8)


def screen(win):
    """224x192 window indices -> 256x224 screen indices."""
    s = np.zeros((SCREEN_H, SCREEN_W), np.uint8)
    s[WY:WY + E.WIN_H, WX:WX + E.WIN_W] = win
    return s


def window(scr):
    return scr[WY:WY + E.WIN_H, WX:WX + E.WIN_W]


def write_audio(path, v):
    """v: samples -126..126 (what the RF5C164 plays) -> 8-bit unsigned WAV at 14.4 kHz."""
    w = wave.open(path, 'wb'); w.setnchannels(1); w.setsampwidth(1); w.setframerate(E.AUD_RATE)
    w.writeframes((np.asarray(v, int) + 128).astype(np.uint8).tobytes()); w.close()


def read_audio(path, nframes):
    """-> RF5C164 sign-magnitude bytes, 1200 per frame (cut or padded with silence to nframes)."""
    w = wave.open(path)
    if w.getframerate() != E.AUD_RATE or w.getnchannels() != 1:
        raise ValueError(f'{path}: must be mono {E.AUD_RATE} Hz')
    raw = w.readframes(w.getnframes())
    if w.getsampwidth() == 1:
        v = np.frombuffer(raw, np.uint8).astype(int) - 128
    else:
        v = np.rint(np.frombuffer(raw, '<i2') / 256).astype(int)
    n = nframes * E.AUD_PER_FRAME
    v = np.clip(np.pad(v[:n], (0, max(0, n - len(v)))), -126, 126)
    return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)


def load_unit(d):
    return json.load(open(os.path.join(d, 'unit.json')))

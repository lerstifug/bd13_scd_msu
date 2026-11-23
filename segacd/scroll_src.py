"""Scrolling scenes in the 3DO data: camera track (MOVE) and the tall virtual background (BACK + MVBG strips).

  python scroll_src.py BD04_001 96 273     -> out/<clip>_<first>_virtual.png and a summary

A MOVE chunk holds the camera position (x, y as signed 16-bit, 3DO pixels; y < 0 = moved up).  When the camera
moves, the frame carries an MVBG strip with exactly the newly exposed rows, at y = the new camera y, so BACK
(rows 0..215) plus the strips form one virtual background spanning rows min(y)..215.
"""
import csv, os, struct, sys
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
B3 = os.path.join(HERE, '..', 'bd13', '3do')
sys.path.insert(0, B3)


def clip_dir(clip):
    """A clip's step 1 export folder (disc 1, or disc 2 for the clips only on disc 2)."""
    d = os.path.join(B3, 'extracted', 'disc1', clip)
    return d if os.path.isdir(d) else os.path.join(B3, 'extracted', 'disc2', clip)


def camera_track(clip, first, last):
    """{frame: (x, y)} from the MOVE chunks (frames without MOVE: (0, 0)).  Read from the step 1 export
    (extracted/disc<n>/<clip>/camera.json, see export_3do.py) when it is there, else from the disc."""
    p = os.path.join(clip_dir(clip), 'camera.json')
    if os.path.exists(p):
        import json
        cam = json.load(open(p))
        return {f: tuple(cam[f]) for f in range(first, last + 1)}
    import opera
    from extract_bd13 import DISCS, frames
    disc = opera.Disc(DISCS[0])
    for t, name, ptr, nb, *_ in disc.tree():
        if t == 2 and name.upper() == clip + '.DAT':
            data = disc.read(ptr, nb)
            break
    track = {}
    for fi, (off, ch) in enumerate(frames(data)):
        if first <= fi <= last:
            mv = [b for tg, b in ch if tg == 'MOVE']
            track[fi] = struct.unpack('>hh', mv[0]) if mv else (0, 0)
    return track


def virtual_bg2d(clip, first, last, frames=None):
    """The scene's whole virtual background: BACK/BAC8 of frame first at (0, 0) plus the strips (MVBG/MVB8) of
    frames first..last at their x, y.  Returns (image (H,W,3) uint8, x of its first column, y of its first row).
    frames: only the strips of these frames (the frames a path plays): a stream can hold another shot between a
    scene's frames (BD03_006 frames 143-152, a shot of its own the main path skips) whose strips would otherwise
    land in this scene's background."""
    d = clip_dir(clip)
    back = next(os.path.join(d, f'{t}_{first:05d}.png') for t in ('BACK', 'BAC8')
                if os.path.exists(os.path.join(d, f'{t}_{first:05d}.png')))
    back = np.array(Image.open(back).convert('RGB'))
    strips = []
    names = [n for n in os.listdir(d) if n.startswith(('MVBG_x', 'MVB8_x'))]   # MVBG_x<x>_y<y>_<frame>.png
    for f in range(first, last + 1):
        if frames is not None and f not in frames:
            continue
        for n in names:
            if n.endswith(f'_{f:05d}.png'):
                _, x, y, _ = n.split('_')
                strips.append((int(x[1:]), int(y[1:]), np.array(Image.open(os.path.join(d, n)).convert('RGB'))))
    left = min([0] + [x for x, y, s in strips]); top = min([0] + [y for x, y, s in strips])
    right = max([288] + [x + s.shape[1] for x, y, s in strips])
    bottom = max([216] + [y + s.shape[0] for x, y, s in strips])
    img = np.zeros((bottom - top, right - left, 3), np.uint8)
    img[-top:216 - top, -left:288 - left] = back
    for x, y, s in strips:
        img[y - top:y - top + s.shape[0], x - left:x - left + s.shape[1]] = s
    return img, left, top


def virtual_bg(clip, first, last, frames=None):
    """Vertical scrolling: (image rows top..215 as an (H,288,3) uint8 array, y of its first row)."""
    img, left, top = virtual_bg2d(clip, first, last, frames)
    return img[:, -left:288 - left], top


GRAD_DEV = 3.0      # a still background whose pixels are this close to their line's mean (0-255, mean absolute
                    # difference) is a plain vertical gradient (the revive clips, black backgrounds)


def _srgb2lin(a):
    a = np.asarray(a, np.float64) / 255
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _lin2srgb(a):
    a = np.clip(a, 0, 1)
    return 255 * np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)


def monotone(y):
    """The closest (least squares) sequence to y that only rises or only falls (whichever fits better):
    pool adjacent violators."""
    def up(v):
        blocks = []                                                 # [mean, count]
        for x in v:
            blocks.append([x, 1])
            while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
                m2, n2 = blocks.pop()
                m1, n1 = blocks.pop()
                blocks.append([(m1 * n1 + m2 * n2) / (n1 + n2), n1 + n2])
        return np.concatenate([[m] * n for m, n in blocks])
    y = np.asarray(y, np.float64)
    a, b = up(y), -up(-y)
    return a if ((a - y) ** 2).sum() <= ((b - y) ** 2).sum() else b


def smooth(y, sigma):
    """y blurred along its length (edges held), which keeps it one-way."""
    k = np.exp(-0.5 * (np.arange(-int(3 * sigma), int(3 * sigma) + 1) / sigma) ** 2)
    k /= k.sum()
    r = len(k) // 2
    return np.convolve(np.pad(y, r, mode='edge'), k, 'valid')


def gradient_lines(img):
    """A still 3DO background (sRGB (h,w,3)) that is a plain vertical gradient: its line colours (h,3), sRGB
    0-255 floats, else None.  The line means are noisy (the 3DO dithers its gradients), so each channel becomes the
    closest curve that only goes one way, softened a little: rounded to any depth it steps once per level, with no
    single lines of another colour."""
    a = np.asarray(img, np.float64)[:, :, :3]
    if np.abs(a - a.mean(1, keepdims=True)).mean() > GRAD_DEV:
        return None
    m = _lin2srgb(_srgb2lin(a).mean(1))
    return np.stack([np.clip(smooth(monotone(m[:, c]), 2.0), 0, 255) for c in range(3)], 1)


if __name__ == '__main__':
    clip, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    tr = camera_track(clip, a, b)
    img, top = virtual_bg(clip, a, b)
    ys = [tr[f][1] for f in sorted(tr)]
    moving = [f for f in sorted(tr) if f > a and tr[f] != tr[f - 1]]
    print(f'{clip} frames {a}-{b}: camera y {max(ys)}..{min(ys)}, moving in frames {moving[0]}-{moving[-1]} '
          f'({len(moving)} frames), largest step {max(abs(tr[f][1] - tr[f - 1][1]) for f in moving)} px')
    print(f'virtual background {img.shape[1]}x{img.shape[0]} (rows {top}..215)')
    out = os.path.join(HERE, 'out', f'{clip}_{a}_virtual.png')
    Image.fromarray(img).save(out)
    print('wrote', out)
    rows = [r for r in csv.DictReader(open(os.path.join(B3, 'extracted', 'manifest.csv')))
            if r['disc'] == '1' and r['file'] == clip + '.DAT']
    d = clip_dir(clip)
    sizes = []
    for f in range(a, b + 1):
        p = os.path.join(d, f'CL16_{f:05d}.png')
        if os.path.exists(p):
            al = np.array(Image.open(p))[:, :, 3] > 0
            sizes.append((f, int(al.sum()), np.argwhere(al).min(0).tolist(), np.argwhere(al).max(0).tolist()))
    print('cel frames', len(sizes), 'opaque px max', max(s[1] for s in sizes), 'bbox examples', sizes[::30])

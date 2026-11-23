"""Extract every bitmap from Brain Dead 13 (3DO) FMV .DAT streams.

Usage: python extract_bd13.py [out_dir]
Chunk types handled: BACK (16bpp background), BAC8 (8bpp background), MVBG/MVB8
(background strips), CL16 (packed 16bpp sprite), CELL (packed 8bpp sprite).
Files: <out>/disc<n>/<clip>/<TAG>_<frame>.png (e.g. CL16_00030.png), strips <TAG>_x<x>_y<y>_<frame>.png - the frame
number last, so Aseprite / ffmpeg see each chunk type of a clip (its cels above all) as one numbered sequence.
"""
import sys, os, struct, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import opera

HERE = os.path.dirname(os.path.abspath(__file__))
DISCS = [os.path.join(HERE, 'BrainDead 13 (USA) (Disc 1) (RE-1).bin'),
         os.path.join(HERE, 'BrainDead 13 (USA) (Disc 2).bin')]
if os.path.exists(os.path.join(HERE, 'discs.json')):     # written by build_assets.py: the user's own disc images
    import json as _json
    DISCS = _json.load(open(os.path.join(HERE, 'discs.json')))

def frames(data):
    p = 0
    while p + 44 <= len(data) and data[p+32:p+36] == b'FORM':
        ln = struct.unpack('>I', data[p:p+4])[0]
        end = p + 40 + struct.unpack('>I', data[p+36:p+40])[0]
        q = p + 44; ch = []
        while q + 8 <= end:
            tg = data[q:q+4].decode('latin1'); sz = struct.unpack('>I', data[q+4:q+8])[0]
            ch.append((tg, data[q+8:q+8+sz])); q += 8 + ((sz + 3) & ~3)
        yield p, ch
        nxt = p + ln
        if ln == 0 or data[nxt+32:nxt+36] != b'FORM':      # resync after a malformed length
            m = data.find(b'BDSQ', p + 48)
            while m != -1 and data[m-8:m-4] != b'FORM': m = data.find(b'BDSQ', m + 1)
            if m == -1: break
            nxt = m - 40
        p = nxt

def chunk_png(tag, frame, xy=None):
    """File name of a frame's bitmap chunk, frame number last: BACK_00000.png, CL16_00030.png; background strips
    MVBG_x<x>_y<y>_00100.png (the strip's place in the virtual background, signed 3DO pixels)."""
    return f'{tag}_{frame:05d}.png' if xy is None else f'{tag}_x{xy[0]}_y{xy[1]}_{frame:05d}.png'

def rgb555(v):
    v = v.astype(np.uint16)
    return np.dstack([((v >> 10) & 31) << 3 | ((v >> 12) & 7), ((v >> 5) & 31) << 3 | ((v >> 7) & 7),
                      (v & 31) << 3 | ((v >> 2) & 7)]).astype(np.uint8)

def rgb332(v):
    """8-bit uncoded cel pixels (CELL chunks): direct colour R3 G3 B2, not palette indices (no PLUT is stored;
    the background's BPAL gives wrong colours).  Expanded to 5 bits per channel the way the 3DO does (bits
    repeated), then to 8 bits like rgb555."""
    v = v.astype(np.uint16)
    r, g, b = (v >> 5) & 7, (v >> 2) & 7, v & 3
    return rgb555(((r << 2 | r >> 1) << 10) | ((g << 2 | g >> 1) << 5) | (b << 3 | b << 1 | b >> 1))

def menu_cel(b, w=288, h=216):
    """The menu cels (PAUSE.CEL, SAVESAVE/SAVECONT/SAVEEXIT.CEL): a 256-entry RGB555 table stored as 256 high bytes
    then 256 low bytes, then rows of packets (2-bit type, 6-bit count - 1: 1 literal bytes, 2 transparent,
    3 one byte repeated; type 0 ends the row), no padding.  -> RGBA (h, w, 4)."""
    tab = rgb555((np.frombuffer(b[0:256], np.uint8).astype(np.uint16) << 8) | np.frombuffer(b[256:512], np.uint8))
    tab = tab.reshape(256, 3)
    out = np.zeros((h, w, 4), np.uint8)
    p, y = 512, 0
    while p < len(b) and y < h:
        x = 0
        while p < len(b):
            t = b[p]; p += 1; ty, n = t >> 6, (t & 63) + 1
            if ty == 0: break
            k = max(0, min(n, w - x))
            if ty == 1:
                out[y, x:x + k, :3] = tab[np.frombuffer(b[p:p + k], np.uint8)]; out[y, x:x + k, 3] = 255; p += n
            elif ty == 3:
                out[y, x:x + k, :3] = tab[b[p]]; out[y, x:x + k, 3] = 255; p += 1
            x += n
        y += 1
    return out

def unpack_cel(b, bpp):
    """3DO packed CEL: per row a 16-bit offset (words-2), then packets
    00=end 01=literal 10=transparent 11=repeat, 6-bit count-1."""
    x, y, w, h = struct.unpack('>HHHH', b[:8])
    pb = bpp // 8
    idx = np.zeros((h, w), np.uint16); mask = np.zeros((h, w), bool)
    p = 8
    for r in range(h):
        if p + 2 > len(b): break
        end = p + (struct.unpack('>H', b[p:p+2])[0] + 2) * 4
        q = p + 2; col = 0
        while q < end and q < len(b):
            t = b[q]; q += 1; typ = t >> 6; n = (t & 63) + 1
            if typ == 0: break
            if typ == 1:
                px = np.frombuffer(b[q:q+n*pb], '>u2' if pb == 2 else np.uint8); q += n * pb
                k = max(0, min(len(px), w - col)); idx[r, col:col+k] = px[:k]
                mask[r, col:col+len(px)] = True; col += n
            elif typ == 2: col += n
            else:
                v = struct.unpack('>H', b[q:q+2])[0] if pb == 2 else b[q]; q += pb
                idx[r, col:col+n] = v; mask[r, col:col+n] = True; col += n
        p = end
    return idx, mask

def deint(a):
    """Backgrounds store two scanlines pixel-interleaved: stored[k,0::2]=row 2k, stored[k,1::2]=row 2k+1."""
    h, w2 = a.shape[:2]; t = np.zeros((h * 2, w2 // 2) + a.shape[2:], a.dtype)
    t[0::2] = a[:, 0::2]; t[1::2] = a[:, 1::2]; return t

def save(path, arr):
    Image.fromarray(arr).save(path, compress_level=1)

def main(out, only=None):
    gray = np.stack([np.arange(256, dtype=np.uint8)] * 3, 1)
    rows = []
    for dn, dp in enumerate(DISCS, 1):
        disc = opera.Disc(dp)
        for t, name, ptr, nb, *_ in disc.tree():
            if t != 2 or not name.upper().endswith('.DAT'): continue
            if only and name[:-4].upper() not in only: continue
            d = os.path.join(out, f'disc{dn}', name[:-4]); os.makedirs(d, exist_ok=True)
            data = disc.read(ptr, nb); pal = gray
            for fi, (off, ch) in enumerate(frames(data)):
                info = ''; tags = []
                for tg, b in ch:
                    tags.append(tg); base = os.path.join(d, chunk_png(tg, fi))[:-4]
                    if tg == 'INFO': info = b.rstrip(b'\0').decode('latin1')
                    elif tg == 'BPAL' and len(b) >= 2:
                        # (a BPAL can be shorter than 256 colours: exactly the colours its BAC8 uses)
                        pal = pal.copy()
                        pal[:len(b) // 2] = rgb555(np.frombuffer(b[:len(b) // 2 * 2], '>u2')).reshape(-1, 3)
                    elif tg == 'BACK':
                        save(base + '.png', deint(rgb555(np.frombuffer(b[8:], '>u2').reshape(108, 576))))
                    elif tg == 'BAC8':
                        save(base + '.png', deint(pal[np.frombuffer(b[8:], np.uint8).reshape(108, 576)]))
                    elif tg in ('MVBG', 'MVB8'):
                        x, y, w, h = struct.unpack('>hhHH', b[:8])
                        if tg == 'MVBG': a = rgb555(np.frombuffer(b[8:], '>u2').reshape(h // 2, w * 2))
                        else: a = pal[np.frombuffer(b[8:], np.uint8).reshape(h // 2, w * 2)]
                        save(os.path.join(d, chunk_png(tg, fi, (x, y))), deint(a))
                    elif tg in ('CL16', 'CELL'):
                        idx, mask = unpack_cel(b, 16 if tg == 'CL16' else 8)
                        rgb = rgb555(idx) if tg == 'CL16' else rgb332(idx)
                        save(base + '.png', np.dstack([rgb, mask.astype(np.uint8) * 255]))
                rows.append((dn, name, fi, off, info, ' '.join(tags)))
            print(dn, name, fi + 1, 'frames', flush=True)
    if only:                                         # keep the other clips' manifest rows
        mf = os.path.join(out, 'manifest.csv')
        old = list(csv.reader(open(mf)))[1:] if os.path.exists(mf) else []
        done = {(str(r[0]), r[1]) for r in rows}
        rows = [r for r in old if (r[0], r[1]) not in done] + rows
        rows.sort(key=lambda r: (int(r[0]), r[1], int(r[2])))
    disc = opera.Disc(DISCS[0])                      # standalone 16bpp screens (same interleave)
    for t, name, ptr, nb, *_ in disc.tree():
        if t == 2 and name.upper().endswith('.RAW') and nb == 124416:
            save(os.path.join(out, name[:-4] + '.png'),
                 deint(rgb555(np.frombuffer(disc.read(ptr, nb), '>u2').reshape(108, 576))))
    with open(os.path.join(out, 'manifest.csv'), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['disc', 'file', 'frame', 'offset', 'info', 'chunks']); w.writerows(rows)

if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'extracted'))

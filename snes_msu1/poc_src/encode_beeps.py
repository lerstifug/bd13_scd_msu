"""The 3DO button beeps for the SNES sound chip:  python encode_beeps.py [--test]
-> poc_src/spc.inc (ROM bank 2: SpcImage, the sound RAM image from $0200 that player.asm uploads at boot) and
   build/beeps_test.spc (a snapshot that plays both beeps by itself: --test renders it with ffmpeg's libgme and checks
   it against the 3DO samples).

3DO BEEPS.BIN (as segacd/beeps.py): "BEEP", u32 rate 44100, u32 bytes, u32 offset of the second sound, u32 0,
then signed 8-bit stereo with both channels the same and every sample stored twice: 22050 Hz mono material.  Sound 0
is the good beep, 1 the bad one.  They stay mono at 22050 Hz (the voice plays them at pitch $0B06) and are BRR
encoded: per 16-sample block the filter and range that decode closest, the last block with the end flag (the voice
falls silent there).  Sound RAM: the driver (spc_beeps.asm) at $0200, the sample directory at $0300 (start, loop
per sample), the samples from $0400.
"""
import os, struct, subprocess, sys
import numpy as np
import build as B

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', '..', 'bd13', '3do', 'extracted', 'cheat', 'BEEPS.BIN')
DIR, SAMPLES = 0x0300, 0x0400


def sounds():
    b = open(SRC, 'rb').read()
    tag, rate, size, split, _ = struct.unpack('>4s4I', b[:20])
    assert tag == b'BEEP' and rate == 44100, (tag, rate)
    pcm = np.frombuffer(b[20:20 + size], np.int8)[0::2][0::2].astype(np.int32) * 256   # left, undoubled: 22050 Hz
    return [pcm[:split // 4], pcm[split // 4:]]


def clamp16(v):
    return max(-32768, min(32767, v))


def brr(x):
    """16-bit samples -> BRR blocks (9 bytes per 16 samples), decoding as the DSP does (15-bit, filters 0-3)."""
    x = np.concatenate([x, np.zeros(-len(x) % 16, np.int32)])
    out, p1, p2 = bytearray(), 0, 0
    nblocks = len(x) // 16
    for k in range(nblocks):
        blk = [int(v) // 2 for v in x[16 * k:16 * k + 16]]          # (the DSP works in 15 bits)
        best = None
        for f in ((0,) if k == 0 else (0, 1, 2, 3)):
            for r in range(13):
                q1, q2, nib, err = p1, p2, [], 0
                for s in blk:
                    if f == 0:
                        pred = 0
                    elif f == 1:
                        pred = q1 + (-q1 >> 4)
                    elif f == 2:
                        pred = 2 * q1 + ((-3 * q1) >> 5) - q2 + (q2 >> 4)
                    else:
                        pred = 2 * q1 + ((-13 * q1) >> 6) - q2 + ((3 * q2) >> 4)
                    n = max(-8, min(7, int(round(2 * (s - pred) / (1 << r)))))  # (decoded as (n << r) >> 1)
                    v = clamp16(pred + ((n << r) >> 1))
                    v = ((v & 0x7FFF) ^ 0x4000) - 0x4000                   # 15-bit wrap, as the DSP
                    err += (v - s) ** 2
                    nib.append(n)
                    q2, q1 = q1, v
                if best is None or err < best[0]:
                    best = (err, f, r, nib, q1, q2)
        _, f, r, nib, p1, p2 = best
        head = r << 4 | f << 2 | (1 if k == nblocks - 1 else 0)
        out.append(head)
        for i in range(0, 16, 2):
            out.append((nib[i] & 15) << 4 | (nib[i + 1] & 15))
    return bytes(out)


def brr_decode(data):
    """BRR -> 16-bit samples as the DSP decodes them (for the check)."""
    out, p1, p2 = [], 0, 0
    for k in range(0, len(data), 9):
        h = data[k]
        r, f = h >> 4, h >> 2 & 3
        for byte in data[k + 1:k + 9]:
            for n in (byte >> 4, byte & 15):
                n = n - 16 if n >= 8 else n
                s = (n << r) >> 1 if r <= 12 else (-2048 if n < 0 else 0)
                if f == 1:
                    s += p1 + (-p1 >> 4)
                elif f == 2:
                    s += 2 * p1 + ((-3 * p1) >> 5) - p2 + (p2 >> 4)
                elif f == 3:
                    s += 2 * p1 + ((-13 * p1) >> 6) - p2 + ((3 * p2) >> 4)
                s = clamp16(s)
                s = ((s & 0x7FFF) ^ 0x4000) - 0x4000
                out.append(s * 2)
                p2, p1 = p1, s
    return np.array(out)


def driver(autoplay=False):
    """spc_beeps.asm assembled: 256 bytes for $0200."""
    obj = os.path.join(B.BUILD, 'spc_beeps.o')
    link = os.path.join(B.BUILD, 'spc_beeps.link')
    out = os.path.join(B.BUILD, 'spc_beeps.bin')
    os.makedirs(B.BUILD, exist_ok=True)
    subprocess.run([B.tool('wla-spc700')] + (['-DAUTOPLAY'] if autoplay else []) +
                   ['-o', obj, 'spc_beeps.asm'], check=True, cwd=HERE)
    open(link, 'w').write(f'[objects]\n{obj}\n')
    subprocess.run([B.tool('wlalink'), '-r', link, out], check=True)
    d = open(out, 'rb').read()
    assert len(d) == 256, len(d)
    return d


def image(autoplay=False):
    """Sound RAM from $0200: driver, directory, samples; and the samples' BRR."""
    blobs = [brr(x) for x in sounds()]
    d = bytearray(driver(autoplay))
    d += bytes(DIR - 0x200 - len(d))
    at, table = SAMPLES, b''
    for b in blobs:
        table += struct.pack('<HH', at, at)
        at += len(b)
    d += table + bytes(SAMPLES - DIR - len(table)) + b''.join(blobs)
    return bytes(d), blobs


def write_spc(img, path):
    """An SPC file: the 256-byte header (PC $0200), 64 KB of sound RAM, DSP registers (left 0: the driver sets them)."""
    head = bytearray(256)
    head[:33] = b'SNES-SPC700 Sound File Data v0.30'
    head[33:35] = b'\x1a\x1a'
    head[35] = 27                                                   # no ID666 tag
    head[36] = 30
    head[37:39] = struct.pack('<H', 0x0200)                         # PC
    head[43] = 0xEF                                                 # SP
    ram = bytearray(65536)
    ram[0x200:0x200 + len(img)] = img
    ram[0xF1] = 0x80                                                # (control: the boot ROM readable, timers off)
    dsp = bytearray(128)
    dsp[0x6C] = 0xE0                                                # (reset state until the driver writes FLG)
    open(path, 'wb').write(bytes(head) + bytes(ram) + bytes(dsp) + bytes(64) + bytes(64))


def check(blobs):
    """Render the test snapshot with ffmpeg (libgme) and find both beeps in it."""
    spc = os.path.join(B.BUILD, 'beeps_test.spc')
    wav = os.path.join(B.BUILD, 'beeps_test.wav')
    subprocess.run([r'C:\ProgramData\ffmpeg\ffmpeg.exe', '-hide_banner', '-loglevel', 'error', '-y', '-i', spc, '-t',
                    '1.5', '-ac', '1', '-ar', '32000', wav], check=True)
    import wave
    w = wave.open(wav)
    y = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float64)
    ok = True
    for k, (x, b) in enumerate(zip(sounds(), blobs)):
        n = len(x) * 32000 // 22050
        ref = np.interp(np.arange(n) * 22050 / 32000, np.arange(len(x)), x.astype(np.float64))
        c = np.correlate(y, ref, 'valid')
        e = np.sqrt(np.convolve(y ** 2, np.ones(n), 'valid'))
        r = c / (np.linalg.norm(ref) * e + 1e-9)
        i = int(np.argmax(r))
        dec = brr_decode(b)[:len(x)]
        snr = 10 * np.log10((x.astype(float) ** 2).sum() / ((dec - x) ** 2).sum())
        level = np.sqrt((y[i:i + n] ** 2).mean()) / np.sqrt((ref ** 2).mean())
        print(f"beep {k} ({'good' if k == 0 else 'bad'}): {len(x)} samples, BRR {len(b)} bytes, BRR against the 3DO "
              f"{snr:.1f} dB; in the render at {i / 32000:.2f} s, match r {r[i]:.3f}, level x{level:.2f}")
        ok &= r[i] > 0.9
    return ok


def main():
    img, blobs = image(autoplay=True)
    write_spc(img, os.path.join(B.BUILD, 'beeps_test.spc'))
    img, blobs = image()
    lines = ['; made by encode_beeps.py: sound RAM from $0200 (spc_beeps.asm, directory $0300, BRR samples $0400)',
             f'.DEFINE SPC_SIZE {len(img)}', 'SpcImage:']
    lines += ['  .db ' + ', '.join(str(v) for v in img[i:i + 32]) for i in range(0, len(img), 32)]
    open(os.path.join(HERE, 'spc.inc'), 'w').write('\n'.join(lines) + '\n')
    print(f'spc.inc: {len(img)} bytes of sound RAM (driver, directory, beeps {len(blobs[0])} + {len(blobs[1])} bytes BRR)')
    if '--test' in sys.argv:
        sys.exit(0 if check(blobs) else 1)


if __name__ == '__main__':
    main()

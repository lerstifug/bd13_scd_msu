"""The button sounds from the 3DO disc (BEEPS.BIN) as RF5C164 wave RAM:  python beeps.py  -> build/BEEPS.BIN, build/BEEPS.INC

3DO BEEPS.BIN: "BEEP", u32 rate (44100), u32 data bytes, u32 byte offset of the second sound, u32 0; then signed
8-bit stereo, both channels the same and every sample stored twice (22 kHz material).  The 3DO program plays it with
fixedstereo8.dsp at the given rate ("using beeps G + B, Rate" in launchme).  Sound 1 is the "good" beep (same
samples as the DOS BEEPG.RAW), sound 2 the "bad" one (DOS BEEPB.RAW).  They are resampled to the chip's own rate (FD = $0800, 32,552 Hz) and laid out for
wave RAM address BEEP_BASE: a silent loop (0, $FF) that a finished beep falls into, then each beep starting on a
256-byte boundary (the chip's start register holds address bits 8-15), ended by $FF.  The player copies the file
into wave RAM once at boot (app.s) and plays the beeps on channel 1."""
import os, struct, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'bd13', '3do', 'extracted', 'cheat', 'BEEPS.BIN')
BUILD = os.path.join(HERE, 'build')
BEEP_BASE = 0x4000                  # above the video audio ring (0 - 14400)
RATE = 32552                        # FD = $0800: one wave RAM byte per output sample
GAIN = 1.0


def resample(x, src, dst):
    """Frequency-domain resampling (nothing above the new Nyquist limit folds back)."""
    n = int(round(len(x) * dst / src))
    X = np.fft.rfft(x)
    Y = np.zeros(n // 2 + 1, complex)
    k = min(len(X), len(Y))
    Y[:k] = X[:k]
    return np.fft.irfft(Y, n) * n / len(x)


def trim(x, th=0.01):
    loud = np.nonzero(np.abs(x) > th)[0]
    return x[loud[0]:loud[-1] + 1]


def sign_mag(x):
    v = np.clip(np.rint(x * GAIN * 127), -126, 126).astype(int)
    return bytes(((0x80 if s >= 0 else 0x00) | min(abs(s), 0x7E)) for s in v)   # $FF is the loop marker


def build():
    b = open(SRC, 'rb').read()
    tag, rate, size, split, _ = struct.unpack('>4s4I', b[:20])
    assert tag == b'BEEP', tag
    pcm = np.frombuffer(b[20:20 + size], np.int8)[::2] / 128.0          # left channel (right is the same)
    sounds = {'GOOD': pcm[:split // 2], 'BAD': pcm[split // 2:]}
    blob = bytearray([0x80, 0xFF])                                      # silent loop at BEEP_BASE
    inc = [f'BEEP_BASE       equ     ${BEEP_BASE:04X}           ; wave RAM address of build/BEEPS.BIN (beeps.py)']
    for name, x in sounds.items():
        y = sign_mag(resample(trim(x), rate, RATE)) + b'\xFF'
        blob += bytes(-len(blob) % 256)
        inc.append(f'BEEP_{name:4s}       equ     ${(BEEP_BASE + len(blob)) >> 8:02X}             '
                   f'; start register: {len(y) - 1} samples, {(len(y) - 1) / RATE * 1000:.0f} ms')
        blob += y
    inc.append(f'BEEP_SIZE       equ     {len(blob)}')
    os.makedirs(BUILD, exist_ok=True)
    open(os.path.join(BUILD, 'BEEPS.BIN'), 'wb').write(blob)
    open(os.path.join(BUILD, 'BEEPS.INC'), 'w').write('\n'.join(inc) + '\n')
    return bytes(blob), inc


if __name__ == '__main__':
    blob, inc = build()
    print('\n'.join(inc))

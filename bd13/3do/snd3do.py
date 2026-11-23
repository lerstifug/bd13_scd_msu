"""3DO BrainDead 13 clip audio:  python snd3do.py BD04_011  -> extracted/disc1/<clip>/audio.wav (mono, 22050 Hz)

Coding: signed 8-bit PCM at 22050 Hz (22050 / 12 = 1837.5 samples per video frame).  Checked against the other
3DO candidate, SDX2: as PCM the stereo channels correlate at 0.978, as SDX2 0.79 with three times the clipping.

Layout: an FSND chunk holds 15 frames of audio preloaded at a path start (see logic.py: the main path and each
death scene); every other frame holds one ASND with one frame of audio for the path that frame belongs to.  The
format is per chunk: 1838 bytes per frame = mono, 3676 = stereo (L/R interleaved).  Every path's audio ends
exactly 14 frames ahead of its video.  path_track() gives a path's audio; frame_track() (the file read straight
through, resetting at each FSND) is only right for files with one path.
"""
import os, sys, wave
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import opera
from extract_bd13 import DISCS, frames

RATE = 22050
PER_FRAME = RATE / 12


def chunks(clip, dn=1):
    disc = opera.Disc(DISCS[dn - 1])
    for t, name, ptr, nb, *_ in disc.tree():
        if t == 2 and name.upper() == clip + '.DAT':
            data = disc.read(ptr, nb)
            break
    return [[(tg, b) for tg, b in ch if tg in ('FSND', 'ASND')] for _, ch in frames(data)]


def decode_chunk(b, nframes):
    """One sound chunk carrying nframes frames -> mono float samples (-1..1)."""
    s = np.frombuffer(b, np.int8).astype(np.float32) / 128
    if len(b) / nframes > 2500:                         # 3676 bytes per frame: stereo
        s = s[:len(s) // 2 * 2].reshape(-1, 2).mean(1)
    return s


def path_track(clip, frame_idx, dn=1):
    """Mono audio for one path of a clip (see logic.py): the sound chunks of its frames, in order (an FSND preload
    at the path start, then one frame per ASND), cut to len(frame_idx) frames."""
    ch = chunks(clip, dn)
    parts = []
    for f in frame_idx:
        for tg, b in ch[f]:
            parts.append(decode_chunk(b, 15 if tg == 'FSND' else 1))
    a = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    n = int(round(len(frame_idx) * PER_FRAME))
    return np.pad(a[:n], (0, max(0, n - len(a))))


def frame_track(clip, dn=1):
    """Mono audio for every video frame of the clip, laid end to end (segments reset at each FSND)."""
    ch = chunks(clip, dn)
    out, seg, start = [], None, 0

    def flush(upto):
        if seg is None:
            return
        a = np.concatenate(seg)
        n = int(round((upto - start) * PER_FRAME))
        out.append(np.pad(a[:n], (0, max(0, n - len(a)))))
    for f, cs in enumerate(ch):
        for tg, b in cs:
            if tg == 'FSND':
                flush(f)
                seg, start = [], f
                seg.append(decode_chunk(b, 15))
            elif seg is not None:
                seg.append(decode_chunk(b, 1))
    flush(len(ch))
    return np.concatenate(out)


def extract(clip, dn=1):
    """Write extracted/disc<dn>/<clip>/audio.wav (16-bit mono); returns the path."""
    x = frame_track(clip, dn)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extracted', f'disc{dn}', clip, 'audio.wav')
    w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes((np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()); w.close()
    return out


if __name__ == '__main__':
    for c in sys.argv[1:]:
        print('wrote', extract(c))

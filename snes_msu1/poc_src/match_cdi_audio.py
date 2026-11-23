"""Match every 3DO path's soundtrack to the CD-i release's audio:  python match_cdi_audio.py [unit ...]
-> cdi_audio.json: for each unit (BD02_001_main, ...) the chain of CD-i segments that plays its sound, in order.

The CD-i tracks (bd13/cdi/mpeg_tracks/SQ<seq>ST<scene>SG<segment>.mpg) carry the same mix as the 3DO, in stereo
MPEG-1 Layer II at 44.1 kHz.  A 3DO path can span several CD-i segments, and the CD-i reuses segments between
scenes, so names are only a tie-break: each segment is found by sound.  From the path's start, every CD-i track
is compared with the next few seconds of the 3DO audio (start against start, within MAX_LAG); the best one is then
checked second by second over its whole length, and the search continues where it ends.

A sound that starts inside a CD-i track (some have a second of lead-in) is found by searching every position.
source: cdi (all of the path is on the CD-i, every segment scoring at least GOOD), 3do (part of it is not, or the
CD-i has a different edit: some 2-second 3DO clips cut away where the CD-i carries on), silent.

Each chain entry: track, at (3DO sample at 22050 Hz where the track's sound starts), lag (CD-i sample at 22050 Hz
that lines up with it; the decoder delay makes it about 240), length (samples), score (median per-second r),
worst (lowest per-second r), ties (other tracks that match as well: the CD-i reuses them).
"""
import glob, json, os, subprocess, sys, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
SRC3 = os.path.join(ROOT, 'bd13', '3do', 'extracted')
MPEG = os.path.join(ROOT, 'bd13', 'cdi', 'mpeg_tracks')
CACHE = os.path.join(HERE, '..', 'build', 'cdi22')         # each track's audio, mono 22050 Hz s16le
ALL = os.path.join(HERE, '..', 'build', 'all')
OUT = os.path.join(HERE, 'cdi_audio.json')
def _ffmpeg():
    """BD13_FFMPEG, else tools/ffmpeg in a release folder, else the one this was made with, else the PATH."""
    import shutil
    exe = 'ffmpeg' + ('.exe' if os.name == 'nt' else '')
    for p in (os.environ.get('BD13_FFMPEG'), os.path.join(ROOT, 'tools', 'ffmpeg', exe), r'C:\ProgramData\ffmpeg\ffmpeg.exe'):
        if p and os.path.exists(p):
            return p
    return shutil.which('ffmpeg') or exe


FFMPEG = _ffmpeg()
RATE = 22050
D = 4                                                       # compare at 22050 / 4 Hz
PROBE = 3 * RATE // D                                       # seconds compared to find a segment
MAX_LAG = 1500 // D                                         # about 70 ms either way
QUIET = 200                                                 # rms (16-bit) under which a stretch is silence
GOOD = 0.9                                                  # a segment scoring under this is not the same sound
MIX = {'BD02_137_main': 1}      # unit: CD-i segments to play, then the 3DO plays on (its death ending is on the CD-i
                                # only in pieces of other tracks)
FADE = 441                      # 10 ms crossfade at 44100 Hz where the 3DO takes over


def decode():
    os.makedirs(CACHE, exist_ok=True)
    for f in sorted(glob.glob(os.path.join(MPEG, '*.mpg'))):
        out = os.path.join(CACHE, os.path.basename(f)[:-4] + '.raw')
        if not os.path.exists(out):
            subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', f, '-vn', '-ac', '1', '-ar',
                            str(RATE), '-f', 's16le', out], check=True)


def small(x):
    return x[:len(x) // D * D].reshape(-1, D).mean(1)


def load_cdi():
    return {os.path.basename(f)[:-4]: small(np.fromfile(f, '<i2').astype(np.float32))
            for f in sorted(glob.glob(os.path.join(CACHE, '*.raw')))}


def load_3do(unit):
    clip, path = unit[:8], unit[9:]
    f = glob.glob(os.path.join(SRC3, 'disc*', clip, f'audio_{path}.wav'))[0]
    w = wave.open(f)
    return np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float32)


def rms(x):
    return float(np.sqrt((x.astype(np.float64) ** 2).mean())) if len(x) else 0.0


def best_lag(a, b):
    """Normalised correlation of a (n) with b at lags -MAX_LAG..MAX_LAG (b[k + lag] ~ a[k]): (r, lag)."""
    n = len(a)
    bb = np.pad(b[:n + MAX_LAG], (MAX_LAG, max(0, n + MAX_LAG - len(b[:n + MAX_LAG]))))
    m = 1 << (len(bb) + n).bit_length()
    c = np.fft.irfft(np.fft.rfft(bb, m) * np.conj(np.fft.rfft(a, m)), m)[:2 * MAX_LAG + 1]
    cs = np.concatenate([[0], np.cumsum(bb.astype(np.float64) ** 2)])
    e = np.sqrt(np.maximum(cs[n:n + 2 * MAX_LAG + 1] - cs[:2 * MAX_LAG + 1], 1e-9))
    r = c / (np.linalg.norm(a) * e + 1e-9)
    k = int(np.argmax(r))
    return float(r[k]), k - MAX_LAG


def anywhere(x, cdi):
    """The best place for x inside any CD-i track: (r, track, lag) with lag >= 0 the CD-i sample lining up with x[0].
    Windows much quieter than x are skipped (they would divide by almost nothing)."""
    x = x - x.mean()
    nx = np.linalg.norm(x)
    best = (-1.0, None, 0)
    for n, b in cdi.items():
        if len(b) < len(x):
            continue
        m = 1 << (len(b) + len(x)).bit_length()
        c = np.fft.irfft(np.fft.rfft(b, m) * np.conj(np.fft.rfft(x, m)), m)[:len(b) - len(x) + 1]
        cs = np.concatenate([[0], np.cumsum(b.astype(np.float64) ** 2)])
        e = np.sqrt(np.maximum(cs[len(x):] - cs[:-len(x)], 1e-9))
        r = np.where(e > 0.3 * nx, c / (nx * e), 0)
        k = int(np.argmax(r))
        if r[k] > best[0]:
            best = (float(r[k]), n, k)
    return best


def per_second(a, b, lag):
    """r for each second where both overlap, with b shifted by lag."""
    s = RATE // D
    out = []
    for i in range(0, len(a) - s // 2, s):
        x = a[i:i + s]
        y = b[max(0, i + lag):i + lag + len(x)]
        if len(y) < len(x) // 2 or i + lag < 0:
            continue
        x = x[:len(y)]
        if rms(x) < QUIET and rms(y) < QUIET:
            continue                                        # both silent: nothing to compare
        out.append(float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9)))
    return out


def match(unit, cdi):
    a = small(load_3do(unit))
    seq, scene = int(unit[2:4]), int(unit[5:8])
    home = f'SQ{seq:03d}ST{scene:03d}'
    chain, pos, prev = [], 0, None
    while len(a) - pos > RATE // D // 2:
        rest = a[pos:]
        if rms(rest) < QUIET:
            break                                           # silence to the end
        probe = rest[:PROBE]
        while rms(probe) < QUIET and len(probe) < len(rest):    # a quiet start: compare a longer stretch
            probe = rest[:len(probe) + PROBE]
        found = []
        for n, b in cdi.items():
            if len(b) < min(len(probe), RATE // D):
                continue
            r, lag = best_lag(probe, b)
            found.append((r, n, lag))
        found.sort(reverse=True)
        if found[0][0] < GOOD:                              # the sound starts inside a CD-i track (it has a lead-in)
            inside = anywhere(probe, cdi)
            if inside[0] > found[0][0] + 0.05:
                found = [inside]
        r0 = found[0][0]
        ties = [f for f in found if f[0] > r0 - 0.01]
        nxt = prev and prev[:-3] + f'{int(prev[-3:]) + 1:03d}'
        r, n, lag = max(ties, key=lambda f: (f[1] == nxt, f[1].startswith(home), -int(f[1][2:5] != f'{seq:03d}'), f[0]))
        b = cdi[n]
        sec = per_second(rest, b, lag)
        use = len(b) - lag                                  # 3DO samples this track covers (lag < 0: it starts late)
        chain.append(dict(track=n, at=pos * D, lag=lag * D, length=len(b) * D,
                          score=round(float(np.median(sec)), 3) if sec else None,
                          worst=round(min(sec), 3) if sec else None, probe=round(r, 3),
                          ties=[t[1] for t in ties if t[1] != n]))
        if r < 0.5:
            chain[-1]['bad'] = True
            break
        pos += use
        prev = n
    total = len(a) * D
    covered = min(total, sum(c['length'] - c['lag'] for c in chain if not c.get('bad')))
    gaps = missing(a, assemble(chain, cdi, len(a))) if chain else []
    if rms(a) < QUIET:
        source = 'silent'
    elif covered >= 0.99 * total and not gaps and all(not c.get('bad') and (c['score'] or 0) >= GOOD for c in chain):
        source = 'cdi'
    else:
        source = '3do'                                      # not all of it is on the CD-i (or it is a different edit)
    out = dict(samples=total, covered=covered, source=source, missing=gaps[:20], chain=chain)
    if unit in MIX and source == '3do':
        out['chain'] = chain = chain[:MIX[unit]]
        out['source'] = 'mix'
        out['until'] = chain[-1]['at'] + chain[-1]['length'] - chain[-1]['lag']     # 3DO sample where the 3DO takes over
    return out


def place(b, at, lag):
    """Where a segment goes: (its samples from the one at the 3DO's position at, that position).  b[lag] lines up
    with the 3DO's sample at; a negative lag means the CD-i sound starts -lag samples after at."""
    return (b[lag:], at) if lag >= 0 else (b, at - lag)


def assemble(chain, cdi, n):
    """The chain's CD-i sound at 22050 / D Hz, n samples long, laid out as cdi_pcm lays it out at 44100."""
    out = np.zeros(n, np.float32)
    for c in chain:
        b, at = place(cdi[c['track']], c['at'] // D, c['lag'] // D)
        k = max(0, min(len(b), n - at))
        out[at:at + k] = b[:k]
        out[at + k:] = 0
    return out


def missing(a, b):
    """Seconds (start times) where the 3DO has sound and the CD-i is near silent or plays something else, leaving out
    the last 0.25 s (some 3DO paths end in a burst of noise that the CD-i rightly lacks) and any stretch where the 3DO
    is pinned at full scale (BD07_008_main ends with 3 s of that)."""
    w = RATE // D // 4
    edge = RATE // D // 25                                  # 40 ms
    ok = np.convolve(np.abs(a) > 30000, np.ones(2 * edge + 1), 'same') == 0   # pinned at full scale, and its edges:
    out = []                                                # an extraction fault, not sound
    for i in range(0, len(a) - 2 * w, w):
        x, y = a[i:i + w], b[i:i + w]
        x, y = x[ok[i:i + w]], y[ok[i:i + w]]
        if len(x) < w // 2:
            continue
        if rms(x) > 1000 and (rms(y) < 0.2 * rms(x) or np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9) < 0.3):
            out.append(round(i / (RATE / D), 2))
    return out


_table = None


def cdi_pcm(unit, n_out, three):
    """A unit's sound from the CD-i as int16 stereo at 44100 Hz, n_out samples long (the 3DO path's length), or None
    when cdi_audio.json does not have it on the CD-i.  Each segment is decoded straight to 44.1 kHz stereo and placed
    where it matched (positions and lags are at 22050 Hz, so doubled).  A mix unit plays the 3DO's sound (three: its
    n_out x 2 samples at 44100 Hz) from 'until' on, crossfading over FADE samples."""
    global _table
    if _table is None:
        _table = json.load(open(OUT)) if os.path.exists(OUT) else {}
    m = _table.get(unit)
    if not m or m['source'] not in ('cdi', 'mix'):
        return None
    if not all(os.path.exists(os.path.join(MPEG, c['track'] + '.mpg')) for c in m['chain']):
        return None                                         # (no CD-i disc extracted: the 3DO's sound)
    out = np.zeros((n_out, 2), np.int16)
    for c in m['chain']:
        p = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', os.path.join(MPEG, c['track'] + '.mpg'),
                            '-vn', '-ac', '2', '-ar', '44100', '-f', 's16le', '-'], check=True, capture_output=True).stdout
        b, at = place(np.frombuffer(p, '<i2').reshape(-1, 2), 2 * c['at'], 2 * c['lag'])
        k = max(0, min(len(b), n_out - at))
        out[at:at + k] = b[:k]
        out[at + k:] = 0                                    # (a later segment fills on from where this one ends)
    if m['source'] == 'mix':
        u = min(2 * m['until'], n_out)
        t = np.linspace(0, 1, FADE)[:, None]
        f = out[u - FADE:u] * (1 - t) + three[u - FADE:u] * t
        out[u - FADE:u] = np.clip(np.rint(f), -32768, 32767)
        out[u:] = np.clip(np.rint(three[u:]), -32768, 32767)
    return out


def units():
    return sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(ALL, 'BD*.json')))


if __name__ == '__main__':
    decode()
    cdi = load_cdi()
    want = sys.argv[1:] or units()
    res = json.load(open(OUT)) if os.path.exists(OUT) and sys.argv[1:] else {}
    for k, u in enumerate(want, 1):
        res[u] = m = match(u, cdi)
        print(f'{k}/{len(want)} {u}: ' + ' + '.join(
            f"{c['track']}({c['score']}{'?' if c.get('bad') else ''})" for c in m['chain'])
            + f"  covers {m['covered'] / max(m['samples'], 1):.0%}: {m['source']}", flush=True)
        if k % 20 == 0:
            json.dump(res, open(OUT, 'w'), indent=1)
    json.dump(res, open(OUT, 'w'), indent=1)

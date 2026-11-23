"""Colour conversion for the Genesis 512-colour grid: palette fitting and pattern dithering.

Palette fitting works on the grid itself: every source pixel is snapped to its nearest of the 512 colours, and each
15-colour line is a weighted k-means over that histogram whose centres are snapped back onto the grid.  Cel
(sprite) pixels weigh more, so their flat cartoon colours tend to become exact palette entries.

Pattern dithering: each pixel either takes its nearest palette colour (solid), or - when no palette colour is
close - alternates between two palette colours in a fixed Bayer pattern so that the light averages (in linear RGB,
as the eye sees it) to the wanted colour.  A flat area therefore becomes one solid colour or one regular pattern,
never noise.  Cels use a high "solid" threshold and quarter-step mixes (regular 2x2-style patterns); backgrounds
use a low threshold and 1/16 steps, since their painterly detail benefits from it.
"""
import numpy as np
import encode_poc as E

LIN = E.srgb2lin(E.GN).astype(np.float32)          # (512,3) linear RGB of the grid colours
LAB = E.LN.astype(np.float32)                       # (512,3) OKLab of the grid colours
LINS = E.srgb2lin(E.GS).astype(np.float32)          # the same 512 codes as shown in a shadowed cell
LABS = E.LS.astype(np.float32)
TABLES = ((LAB, LIN), (LABS, LINS))                 # by mode: 0 = normal, 1 = shadow
M1 = np.array([[.4122214708, .5363325363, .0514459929], [.2119034982, .6806995451, .1073969566],
               [.0883024619, .2817188376, .6299787005]], np.float32)
M2 = np.array([[.2104542553, .7936177850, -.0040720468], [1.9779984951, -2.4285922050, .4505937099],
               [.0259040371, .7827717662, -.8086757660]], np.float32)
# Bayer threshold of each pixel of each cell, (k + 0.5) / 16
THRESH = (E.DITHER + 0.5 + 1 / 32).astype(np.float32)

# solid: below this OKLab distance the nearest colour is used as is; steps: mix ratios in 1/steps;
# contrast: penalty per unit of distance between the two mixed colours (a far-apart pair is a visible checkerboard)
CEL = dict(solid=0.030, steps=4, contrast=0.6)
BG = dict(solid=0.012, steps=16, contrast=0.3)
CEL_WEIGHT = 6.0


def lin2lab(x):
    return np.cbrt(np.maximum(x, 0) @ M1.T) @ M2.T


def snap(lab, table=LAB):
    """Nearest grid code for each OKLab colour (any shape (...,3)); table LABS = as seen in a shadowed cell."""
    f = lab.reshape(-1, 3)
    out = np.empty(len(f), np.int16)
    for i in range(0, len(f), 8192):
        out[i:i + 8192] = ((f[i:i + 8192, None] - table[None]) ** 2).sum(2).argmin(1)
    return out.reshape(lab.shape[:-1])


def fit_line(hist, k=15, it=12):
    """15 grid codes for a weighted histogram over the 512 grid codes."""
    idx = np.nonzero(hist)[0]
    if len(idx) <= k:
        return idx if len(idx) else np.array([0])
    X, w = LAB[idx], hist[idx].astype(np.float64) ** 0.6       # compressed: rarer distinct hues still get entries
    C = [X[np.argmax(w)]]                                       # weighted k-means++ (deterministic: farthest)
    d = ((X - C[0]) ** 2).sum(1)
    for _ in range(k - 1):
        C.append(X[np.argmax(w * d)])
        d = np.minimum(d, ((X - C[-1]) ** 2).sum(1))
    C = np.array(C)
    for _ in range(it):
        a = ((X[:, None] - C[None]) ** 2).sum(2).argmin(1)
        for j in range(k):
            m = a == j
            if m.any():
                C[j] = (X[m] * w[m, None]).sum(0) / w[m].sum()
        C = LAB[snap(C)]                                         # keep the centres on the grid
    return np.unique(snap(C))


def pattern(T, cel, th, S, mode=0):
    """Pattern-dither tiles T (n,64,3 OKLab) with the colours S (grid codes), shown normally (mode 0) or
    shadowed (mode 1).  cel (n,64) bool chooses the cel settings per pixel; th (n,64) Bayer thresholds.
    Returns idx (n,64) into S and the error the eye sees (n,64), as OKLab distance."""
    Slab, Slin = TABLES[mode][0][S], TABLES[mode][1][S]
    n = T.shape[0]
    t = T.reshape(-1, 3)
    d =((t[:, None] - Slab[None]) ** 2).sum(2)                  # (m, |S|)
    K = min(4, len(S))
    near = np.argsort(d, 1)[:, :K]
    e1 = np.sqrt(np.take_along_axis(d, near[:, :1], 1)[:, 0])
    best_i, best_j, best_r, best_e = near[:, 0].copy(), near[:, 0].copy(), np.zeros(len(t), np.float32), e1.copy()
    celf = cel.reshape(-1)
    steps = np.where(celf, CEL['steps'], BG['steps']).astype(np.float32)
    solid = np.where(celf, CEL['solid'], BG['solid'])
    lam = np.where(celf, CEL['contrast'], BG['contrast']).astype(np.float32)
    tlin = lab2lin(t)
    best_c = e1.copy()                                           # cost = seen error + contrast penalty
    for a in range(K):
        for b in range(a + 1, K):
            ci, cj = near[:, a], near[:, b]
            Li, Lj = Slin[ci], Slin[cj]
            v = Lj - Li
            r = np.clip(((tlin - Li) * v).sum(1) / ((v * v).sum(1) + 1e-9), 0, 1)
            r = np.round(r * steps) / steps
            e = np.sqrt(((lin2lab(Li + r[:, None] * v) - t) ** 2).sum(1))
            cost = e + lam * np.sqrt(((Slab[ci] - Slab[cj]) ** 2).sum(1)) * 2 * np.minimum(r, 1 - r)
            better = (cost < best_c) & (r > 0) & (r < 1)
            best_i[better], best_j[better], best_r[better] = ci[better], cj[better], r[better]
            best_e[better], best_c[better] = e[better], cost[better]
    use = (e1 > solid) & (best_c < e1)                           # mix only where the nearest colour is not close
    thf = th.reshape(-1)
    pick = np.where(use & (thf < best_r), best_j, best_i)
    pick = np.where(use, pick, near[:, 0])
    err = np.where(use, best_e, e1)
    return pick.reshape(n, 64), err.reshape(n, 64)


def lab2lin(lab):
    Minv2 = np.linalg.inv(M2.astype(np.float64)).astype(np.float32)
    Minv1 = np.linalg.inv(M1.astype(np.float64)).astype(np.float32)
    lms = (lab @ Minv2.T) ** 3
    return lms @ Minv1.T

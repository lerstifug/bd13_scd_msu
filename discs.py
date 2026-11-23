"""Find and check the disc images the converters need, against their Redump entries (redump.org).

  python discs.py <files or folders ...>      identifies every disc image there and says which builds it allows

A disc is given as its .cue (the .bin it names is checked) or its .bin.  Only the size is read at first; a file of
a known size is then hashed (SHA-1, about a minute per disc) and the result is cached in discs_checked.json, so it
is done once.  A dump that does not match Redump is refused, unless --unverified is given (at your own risk: a bad
dump fails in odd places, hours into a build).
"""
import hashlib, json, os, re, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'discs_checked.json')

# Redump entries (single data track, raw 2352-byte sectors).  disc1 1.0 has a softlock bug that 1.1 fixed; this
# converter was made and tested with 1.1.
KNOWN = {
    'disc1': [
        dict(title='BrainDead 13 (USA) (Disc 1) (RE-1)', system='3DO', version='1.1', redump=76493,
             size=771413664, crc32='8d0d6228', md5='8ea2ab2dbf52642a9cca5958d5ed67c8',
             sha1='995534c88c8df0a6ab16a7a1775d4bf061287c8d'),
        dict(title='BrainDead 13 (USA) (Disc 1)', system='3DO', version='1.0', redump=55469,
             size=771413664, crc32='939a863c', md5='575dd343076bca267b1dc3f3ed2d76f1',
             sha1='6e7c6a74c574941ae471500f7f1c70a47517f53a',
             note='the original release (its softlock bug was fixed in 1.1); the converter was tested with 1.1 only'),
    ],
    'disc2': [
        dict(title='BrainDead 13 (USA) (Disc 2)', system='3DO', version='', redump=55470,
             size=506832480, crc32='747f1ca8', md5='204d700defbc5f65cc078ac89d1a6794',
             sha1='981dbcc139e7cc275c213cd027797fb9a15fcad4'),
    ],
    'cdi': [
        dict(title='BrainDead 13 (Europe)', system='CD-i', version='1.08', redump=67959,
             size=728964768, crc32='32748463', md5='b82bd1a979e2709a5d189ae5bfa204bb',
             sha1='56042f99da19f9a79e1b406ab434ce6c90fb480b'),
    ],
}
SIZES = {e['size'] for v in KNOWN.values() for e in v}
LABEL = {'disc1': '3DO disc 1', 'disc2': '3DO disc 2', 'cdi': 'CD-i disc'}


def track_file(path):
    """A .cue -> the data track it names; a .bin or .iso as it is."""
    if path.lower().endswith('.cue'):
        text = open(path, encoding='latin-1').read()
        m = re.search(r'FILE\s+"([^"]+)"', text) or re.search(r'FILE\s+(\S+)', text)
        if not m:
            sys.exit(f'{path}: no FILE line')
        path = os.path.join(os.path.dirname(os.path.abspath(path)), m.group(1))
    return os.path.abspath(path)


def hashes(path):
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    st = os.stat(path)
    key = f'{path}|{st.st_size}|{int(st.st_mtime)}'
    if key in cache:
        return cache[key]
    print(f'  checking {os.path.basename(path)} ({st.st_size / 2 ** 20:.0f} MB) ...', flush=True)
    md5, sha1, crc = hashlib.md5(), hashlib.sha1(), 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 24)
            if not b:
                break
            md5.update(b); sha1.update(b); crc = zlib.crc32(b, crc)
    h = dict(size=st.st_size, crc32=f'{crc:08x}', md5=md5.hexdigest(), sha1=sha1.hexdigest())
    cache[key] = h
    json.dump(cache, open(CACHE, 'w'), indent=1)
    return h


def identify(path):
    """(kind, Redump entry) for a known dump, else (None, hashes)."""
    p = track_file(path)
    if not os.path.exists(p):
        sys.exit(f'disc image not found: {p}')
    if os.path.getsize(p) not in SIZES:
        return None, dict(size=os.path.getsize(p))
    h = hashes(p)
    for kind, entries in KNOWN.items():
        for e in entries:
            if e['sha1'] == h['sha1']:
                return kind, e
    return None, h


def candidates(paths):
    """Every .cue, .bin and .iso in these files / folders (a .bin a .cue names is taken through its .cue)."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                out += [os.path.join(root, f) for f in files if f.lower().endswith(('.cue', '.bin', '.iso'))]
        else:
            out.append(p)
    cued = {track_file(c) for c in out if c.lower().endswith('.cue')}
    return [c for c in out if c.lower().endswith('.cue') or os.path.abspath(c) not in cued]


def find(paths, need, optional=(), unverified=None):
    """{kind: track file} for the kinds needed (and the optional ones found).  unverified: {kind: path} taken as
    they are (--unverified)."""
    found = dict(unverified or {})
    for c in candidates(paths):
        kind, e = identify(c)
        if kind and kind not in found:
            found[kind] = track_file(c)
            note = f" - {e['note']}" if e.get('note') else ''
            print(f"  {LABEL[kind]}: {os.path.basename(c)} = Redump {e['redump']} {e['title']}"
                  f"{' v' + e['version'] if e['version'] else ''}{note}", flush=True)
        elif kind is None and e['size'] in SIZES:
            print(f'  {os.path.basename(c)}: the size of a Brain Dead 13 disc, but not a Redump dump '
                  f"(SHA-1 {e['sha1']})", flush=True)
    missing = [k for k in need if k not in found]
    if missing:
        lines = []
        for k in missing:
            e = KNOWN[k][0]
            lines.append(f"  {LABEL[k]}: {e['title']} (redump.org/disc/{e['redump']}/), {e['size']} bytes, "
                         f"CRC32 {e['crc32']}, SHA-1 {e['sha1']}")
        sys.exit('missing disc images:\n' + '\n'.join(lines) +
                 '\nGive the .cue (or .bin) files or the folders that hold them.')
    return {k: v for k, v in found.items() if k in need or k in optional}


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        sys.exit(__doc__)
    got = find(args, [], optional=list(KNOWN))
    print('Sega CD build:', 'yes' if {'disc1', 'disc2'} <= set(got) else 'needs both 3DO discs')
    print('SNES build:', ('yes, with the CD-i stereo sound' if 'cdi' in got else 'yes, with the 3DO sound (no CD-i disc)')
          if {'disc1', 'disc2'} <= set(got) else 'needs both 3DO discs')

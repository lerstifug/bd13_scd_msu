"""Build the SNES MSU-1 prototype:  python build.py [clip] [script]   (default BD03_003, no test presses)

  encode_snes.py  -> build/bd13msu.msu (the clip, its death scenes and its revive clip), build/bd13msu-<n>.pcm
                     (their soundtracks), poc_src/units.inc (unit table, decisions, test presses), simulated checks
  player.asm      -> build/bd13msu.sfc (WLA-DX wla-65816 / wlalink from the SNES-IDE checkout)
script: 'good' (every right move) or 'die:N' (miss decision N once, then every right move); none: play it yourself.
Run the files together (same folder, same name): bsnes, an FXPak Pro, or the MiSTer SNES core.
--rom only rebuilds the player (units.inc as it is).
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, '..', 'build')
# WLA-DX: BD13_WLA (a folder), else tools/wla-dx in a release folder, else the SNES-IDE checkout, else the PATH
WLA = next((d for d in (os.environ.get('BD13_WLA'), os.path.join(HERE, '..', '..', 'tools', 'wla-dx'),
                        os.path.join(HERE, '..', 'reference_repos', 'SNES-IDE', 'resources', 'bin', 'windows', 'pvsneslib',
                                     'devkitsnes', 'bin')) if d and os.path.isdir(d)), '')
NAME = 'bd13msu'


def tool(name):
    """A WLA-DX program (wla-65816, wla-spc700, wlalink): in WLA, with .exe on Windows, or by name from the PATH."""
    exe = name + ('.exe' if os.name == 'nt' else '')
    p = os.path.join(WLA, exe)
    return p if WLA and os.path.exists(p) else exe


def rom(where=BUILD):
    os.makedirs(where, exist_ok=True)
    obj = os.path.join(where, 'player.o')
    link = os.path.join(where, 'player.link')
    subprocess.run([tool('wla-65816'), '-o', obj, 'player.asm'], check=True, cwd=HERE)
    open(link, 'w').write(f'[objects]\n{obj}\n')
    out = os.path.join(where, NAME + '.sfc')
    subprocess.run([tool('wlalink'), '-r', link, out], check=True)
    print(f'{out}: {os.path.getsize(out) // 1024} KB')


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '--rom' not in sys.argv:
        sys.argv = [sys.argv[0]] + args
        import encode_snes
        encode_snes.main()
    rom()

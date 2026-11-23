"""Convert your Brain Dead 13 3DO discs into a Sega CD / Mega CD disc image.

  python build_segacd.py <disc images or folders ...> [--workers 3] [--unverified DISC=path] [--redo STEP]

Give both 3DO discs (their .cue or .bin files, or the folders that hold them); they are checked against Redump
(discs.py).  Writes output/segacd/bd13.cue + bd13.bin: burn it, or load the .cue in an emulator or on a MiSTer
(Mega CD core).  Needs tools/vasm and tools/cdboot (see README.md).

Steps (each is kept when done; run the same command again after a stop, --redo STEP to do one again):
  export   bd13/3do/export_3do.py ALL         the 3DO data as PNG / JSON / WAV        about 5 minutes
  bitmaps  segacd/scd_bitmaps.py               Sega CD bitmaps (editable BMPs)         about 1.5 hours, 3 workers
  pack     segacd/scd_pack.py                  the player's streams, checked           about 20 minutes
  disc     segacd/build_disc.py                programs + disc image                    a minute
Disk space: about 25 GB while building.
"""
import argparse, os, shutil, sys
import discs, steps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('discs', nargs='+', help='.cue / .bin files, or folders holding them')
    ap.add_argument('--workers', type=int, default=3, help='parallel conversions (each can use about 3 GB of RAM)')
    ap.add_argument('--unverified', action='append', default=[], metavar='DISC=PATH',
                    help='use a dump that does not match Redump (disc1=..., disc2=...); at your own risk')
    ap.add_argument('--redo', action='append', default=[], choices=('export', 'bitmaps', 'pack', 'disc'))
    a = ap.parse_args()
    steps.check_python()
    sys.path.insert(0, steps.SCD)
    import build_disc as B
    for path, what in ((B.VASM, 'vasm (tools/vasm)'), (B.BOOT, "cdboot's US_BOOT.BIN (tools/cdboot)")):
        if not os.path.exists(path):
            sys.exit(f'{what} not found at {os.path.normpath(path)}: see README.md')
    print('disc images:')
    unv = dict(u.split('=', 1) for u in a.unverified)
    got = discs.find(a.discs, ['disc1', 'disc2'], unverified={k: discs.track_file(v) for k, v in unv.items()})
    steps.forget(*[f'scd_{s}' for s in a.redo] + (['export'] if 'export' in a.redo else []))

    steps.export_3do(got)
    w = str(a.workers)
    if not steps.done('scd_bitmaps'):
        steps.run(steps.SCD, 'scd_bitmaps.py', 'MENUS', label='Sega CD bitmaps: menus')
        steps.run(steps.SCD, 'scd_bitmaps.py', 'CHEAT', label='Sega CD bitmaps: the cheat menu')
        steps.run(steps.SCD, 'scd_bitmaps.py', 'ALL', '--workers', w,
                  label=f'Sega CD bitmaps: every clip ({w} workers, about 1.5 hours)')
        steps.mark('scd_bitmaps')
    if not steps.done('scd_pack'):
        steps.run(steps.SCD, 'scd_pack.py', 'MENUS', label='streams: menus')
        steps.run(steps.SCD, 'scd_pack.py', 'CHEAT', label='streams: the cheat menu')
        steps.run(steps.SCD, 'scd_pack.py', 'ALL', '--workers', str(max(a.workers, 4)),
                  label='streams: every clip, each checked against its bitmaps')
        steps.mark('scd_pack')
    steps.run(steps.SCD, 'build_disc.py', os.path.join('out', 'set_ALL.json'), 'bd13', label='programs and disc image')
    out = os.path.join(steps.HERE, 'output', 'segacd')
    os.makedirs(out, exist_ok=True)
    for ext in ('.bin', '.cue'):
        shutil.move(os.path.join(steps.SCD, 'disc', 'bd13' + ext), os.path.join(out, 'bd13' + ext))
    iso = os.path.join(steps.SCD, 'disc', 'bd13.iso')
    if os.path.exists(iso):
        os.remove(iso)                                      # (the .bin holds the same; 360 MB less)
    print(f'\ndone: {os.path.join(out, "bd13.cue")} (+ bd13.bin)')


if __name__ == '__main__':
    main()

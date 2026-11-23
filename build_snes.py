"""Convert your Brain Dead 13 discs into a SNES MSU-1 game (for an FXPak / SD2SNES, the MiSTer SNES core, bsnes,
Snes9x 1.60+ and other MSU-1 players).

  python build_snes.py <disc images or folders ...> [--workers 4] [--no-cdi] [--folder] [--redo STEP]

Give both 3DO discs, and the CD-i disc too for the CD-i's stereo sound (the same mix; without it the game uses the
3DO's mono sound).  They are checked against Redump (discs.py).  Writes output/snes/BrainDead13-MSU1.zip:
bd13msu.sfc, bd13msu.msu and the bd13msu-<n>.pcm tracks, which must stay together in one folder.  --folder: the
files in output/snes/bd13msu/ instead of a zip.  Needs WLA-DX (tools/wla-dx), and ffmpeg for the CD-i sound
(see README.md).

Steps (each is kept when done; run the same command again after a stop, --redo STEP to do one again):
  export   bd13/3do/export_3do.py ALL         the 3DO data as PNG / JSON / WAV          about 5 minutes
  cdi      bd13/cdi/extract_cdi_mpeg.py       the CD-i's MPEG tracks (its sound)        a few minutes
  menus    snes_msu1/poc_src/encode_*.py      start screen, save menu, cheat menu, beeps a few minutes
  units    snes_msu1/poc_src/bulk.py          every clip and death scene, each checked  1 to 3 hours
  pack     snes_msu1/poc_src/pack_game.py     ROM, MSU data, audio tracks               10 minutes
Disk space: about 12 GB while building, 1.1 GB for the game.
"""
import argparse, os, shutil, sys, zipfile
import discs, steps


def wla_ok():
    sys.path.insert(0, steps.SNES)
    import build as B
    return shutil.which(B.tool('wla-65816')) or os.path.exists(B.tool('wla-65816'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('discs', nargs='+', help='.cue / .bin files, or folders holding them')
    ap.add_argument('--workers', type=int, default=4, help='parallel encoders (each can use 2-3 GB of RAM)')
    ap.add_argument('--no-cdi', action='store_true', help="the 3DO's sound even if a CD-i disc is given")
    ap.add_argument('--folder', action='store_true', help='the game as a folder instead of a zip')
    ap.add_argument('--unverified', action='append', default=[], metavar='DISC=PATH',
                    help='use a dump that does not match Redump (disc1=..., disc2=..., cdi=...); at your own risk')
    ap.add_argument('--redo', action='append', default=[], choices=('export', 'cdi', 'menus', 'units', 'pack'))
    a = ap.parse_args()
    steps.check_python()
    if not wla_ok():
        sys.exit('WLA-DX (wla-65816, wla-spc700, wlalink) not found: put it in tools/wla-dx (see README.md)')
    print('disc images:')
    unv = dict(u.split('=', 1) for u in a.unverified)
    got = discs.find(a.discs, ['disc1', 'disc2'], optional=['cdi'],
                     unverified={k: discs.track_file(v) for k, v in unv.items()})
    use_cdi = 'cdi' in got and not a.no_cdi
    if use_cdi:
        sys.path.insert(0, steps.SNES)
        import match_cdi_audio as M
        if not (os.path.exists(M.FFMPEG) or shutil.which(M.FFMPEG)):
            sys.exit('ffmpeg is needed for the CD-i sound: put it in tools/ffmpeg or on the PATH (or use --no-cdi)')
    steps.forget(*[f'snes_{s}' for s in a.redo] + (['export'] if 'export' in a.redo else []))

    steps.export_3do(got)
    mpeg = os.path.join(steps.CDI, 'mpeg_tracks')
    if use_cdi and not steps.done('snes_cdi', got['cdi']):
        steps.run(steps.CDI, 'extract_cdi_mpeg.py', got['cdi'], label="CD-i: its MPEG tracks (the game's sound in stereo)")
        steps.mark('snes_cdi', got['cdi'])
    elif not use_cdi and os.path.isdir(mpeg):
        shutil.rmtree(mpeg)                                 # (the 3DO's sound for every clip)
        steps.forget('snes_cdi')
    if not steps.done('snes_menus'):
        for s, what in (('encode_title.py', 'start screen'), ('encode_menu.py', 'save menu and PAUSE text'),
                        ('encode_cheat.py', 'cheat menu'), ('encode_beeps.py', 'button beeps (sound chip)')):
            steps.run(steps.SNES, s, label=f'SNES {what}')
        steps.mark('snes_menus')
    if not steps.done('snes_units'):
        steps.run(steps.SNES, 'bulk.py', '--workers', str(a.workers),
                  label=f'SNES streams: every clip and death scene, each checked by a PPU simulation ({a.workers} workers)')
        steps.mark('snes_units')
    sound = 'cdi' if use_cdi else '3do'
    game = os.path.join(steps.HERE, 'snes_msu1', 'build', 'game')
    if not steps.done('snes_pack', sound):
        if os.path.isdir(game):
            for f in os.listdir(game):
                if f.endswith('.pcm'):
                    os.remove(os.path.join(game, f))        # (made again with this sound)
        steps.run(steps.SNES, 'pack_game.py', '--workers', str(a.workers),
                  label=f"ROM, MSU data and audio tracks ({'CD-i stereo' if use_cdi else '3DO'} sound)")
        steps.mark('snes_pack', sound)
    files = ['bd13msu.sfc', 'bd13msu.msu'] + sorted((f for f in os.listdir(game) if f.startswith('bd13msu-') and f.endswith('.pcm')),
                                                    key=lambda f: int(f[8:-4]))
    out = os.path.join(steps.HERE, 'output', 'snes')
    os.makedirs(out, exist_ok=True)
    if a.folder:
        d = os.path.join(out, 'bd13msu')
        os.makedirs(d, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(game, f), os.path.join(d, f))
        print(f'\ndone: {d} ({len(files)} files; keep them together)')
    else:
        z = os.path.join(out, 'BrainDead13-MSU1.zip')
        print('> zip', flush=True)
        with zipfile.ZipFile(z, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:    # (the audio barely compresses)
            for f in files:
                zf.write(os.path.join(game, f), 'bd13msu/' + f)
        print(f'\ndone: {z} ({len(files)} files in bd13msu/; unzip to your SD card and keep them together)')


if __name__ == '__main__':
    main()

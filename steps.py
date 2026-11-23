"""Shared by build_segacd.py and build_snes.py: running the conversion steps, and remembering which are done
(build_state.json), so a build that was stopped goes on from the step it was in."""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'build_state.json')
B3 = os.path.join(HERE, 'bd13', '3do')
CDI = os.path.join(HERE, 'bd13', 'cdi')
SCD = os.path.join(HERE, 'segacd')
SNES = os.path.join(HERE, 'snes_msu1', 'poc_src')


def state():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def done(step, key=''):
    """Is this step done (for these discs / options: key)?"""
    return state().get(step) == (key or True)


def mark(step, key=''):
    s = state()
    s[step] = key or True
    json.dump(s, open(STATE, 'w'), indent=1)


def forget(*steps):
    s = state()
    for k in steps:
        s.pop(k, None)
    json.dump(s, open(STATE, 'w'), indent=1)


def run(cwd, *args, label=None):
    print(f"> {label or ' '.join(args)}", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable] + list(args), cwd=cwd)
    if r.returncode:
        sys.exit(f'failed: {" ".join(args)} (in {os.path.relpath(cwd, HERE)}); fix the cause and run the same command '
                 'again: finished steps are kept')
    print(f'  ({(time.time() - t0) / 60:.1f} min)', flush=True)


def export_3do(discs):
    """Step shared by both builds: the 3DO data in plain files (bd13/3do/extracted/)."""
    key = discs['disc1'] + '|' + discs['disc2']
    if done('export', key):
        print('3DO export: done before (bd13/3do/extracted/)')
        return
    json.dump([discs['disc1'], discs['disc2']], open(os.path.join(B3, 'discs.json'), 'w'), indent=1)
    run(B3, 'export_3do.py', 'ALL', label='3DO export: bitmaps, logic, audio, menus (about 5 minutes)')
    mark('export', key)


def check_python():
    if sys.version_info < (3, 10):
        sys.exit('Python 3.10 or newer is needed')
    try:
        import numpy, PIL                                   # noqa: F401
    except ImportError:
        sys.exit('numpy and Pillow are needed:  python -m pip install -r requirements.txt')

"""Encode every unit of the game for the SNES and check each one:  python bulk.py [--workers 8] [--force] [prefix]

A unit is one path of a clip (the main path or a death scene), as in encode_snes.encode_unit.  For each:
build/all/<unit>.msu (its stream), <unit>.json (shown field of each frame, numbers), and the PPU simulation
compared with what the encoder meant.  Done units are kept unless --force.  Results go to build/all/report.jsonl
(one line per unit, also the failures with their reason), and a summary is printed.
"""
import json, multiprocessing, os, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, '..', 'build', 'all')


def run(unit):
    clip, path = unit
    name = f'{clip}_{path}'
    js = os.path.join(OUT, name + '.json')
    if os.path.exists(js) and '--force' not in sys.argv:
        return json.load(open(js))
    import io, contextlib
    import encode_snes as S
    t0 = time.time()
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            data, shown, shown_at = S.encode_unit(clip, path)
            bad, grabs = S.simulate(data, shown, keep=False)
            del shown
        open(os.path.join(OUT, name + '.msu'), 'wb').write(data)
        r = dict(unit=name, ok=bad == 0, differ=bad, shown_at=shown_at, **S.STATS)
    except BaseException as e:                                      # (sys.exit from the encoder too)
        msg = str(e) if isinstance(e, SystemExit) else ''.join(traceback.format_exception(e)[-2:]).strip()
        r = dict(unit=name, ok=False, error=msg)
    r['secs'] = round(time.time() - t0)
    r['log'] = log.getvalue()[-2000:]
    json.dump(r, open(js, 'w'))
    return r


def main():
    os.makedirs(OUT, exist_ok=True)
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    workers = int(sys.argv[sys.argv.index('--workers') + 1]) if '--workers' in sys.argv else 8
    if '--workers' in sys.argv:
        args.remove(str(workers))
    prefix = args[0] if args else ''
    logic = json.load(open(os.path.join(HERE, '..', '..', 'bd13', '3do', 'extracted', 'logic_ALL.json')))
    units = [(c, p) for c, L in sorted(logic.items()) if c.startswith(prefix) for p in L['paths']]
    units.sort(key=lambda u: -len(logic[u[0]]['paths'][u[1]]))     # longest first
    print(f'{len(units)} units, {workers} workers', flush=True)
    t0 = time.time()
    rows = []
    with multiprocessing.Pool(workers, maxtasksperchild=4) as pool, open(os.path.join(OUT, 'report.jsonl'), 'w') as rep:
        for k, r in enumerate(pool.imap_unordered(run, units), 1):
            rows.append(r)
            rep.write(json.dumps({x: y for x, y in r.items() if x not in ('shown_at', 'log')}) + '\n')
            rep.flush()
            what = r.get('error') or (f"{r['kb']} KB, {r['kbps']} KB/s" + (', DIFFERS' if r['differ'] else '')
                                      + (f", {r['buffered']} buffered" if r['buffered'] else '')
                                      + (f", {r['banded']} banded" if r['banded'] else '')
                                      + (f", {r['late']} late" if r['late'] else '')
                                      + (f", {r['dropped']} dropped" if r['dropped'] else ''))
            print(f'[{k}/{len(units)} {time.time() - t0:.0f} s] {r["unit"]}: {what}', flush=True)
    ok = [r for r in rows if r.get('ok')]
    bad = [r for r in rows if not r.get('ok')]
    print(f'\n{len(ok)} of {len(rows)} units encoded and exact, {sum(r["kb"] for r in ok) / 1024:.0f} MB')
    for kind in ('buffered', 'banded', 'late', 'dropped', 'steps_moved'):
        n = [r for r in ok if r.get(kind)]
        print(f'  {kind}: {len(n)} units ({sum(r[kind] for r in n)} in all)')
    reasons = {}
    for r in bad:
        reasons.setdefault((r.get('error') or f"differs ({r.get('differ')} frames)").split(':')[-1].strip()[:80], []).append(r['unit'])
    for why, us in sorted(reasons.items(), key=lambda x: -len(x[1])):
        print(f'  FAILED {len(us)}: {why}: {" ".join(us[:12])}{" ..." if len(us) > 12 else ""}')


if __name__ == '__main__':
    main()

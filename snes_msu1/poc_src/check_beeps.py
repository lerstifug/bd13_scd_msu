"""Check the button beeps in the interpreter (as the 3DO's launchme input handler):  python check_beeps.py

A move when no decision is open does nothing but the bad beep; pressing the right move again and again before its
window plays the bad beep each time, then the good beep once the window is open, and the clip plays on.  In a route's
window every move chooses again: the last one wins, and one the route does not offer drops the choice.  A window
that runs out with nothing pressed dies without a beep.  Builds a
test ROM (rerun pack_game.py --no-msu --no-pcm for the release ROM)."""
import sys
import check_pause as C

U = C.NAMES['BD03_001_main']                       # its first decision: death, the right move is Right
d = U['decisions'][0]
f = U['fields'][d['frame']]
early = [f - 200, f - 120, f - 60, f - 20]
C.build('BD03_001', [f'BD03_001_main:{x}:right' for x in early + [f + 20]])
cpu = C.boot()
played = C.run(cpu, 2)
beeps = [b[2] for b in cpu.beeps]
C.check('presses before the window: a bad beep each, then the good beep in it', beeps, [1, 1, 1, 1, 0])
C.check('the right move in its window: the clip plays on', played[:2], ['BD03_001_main', U['next'] and C.NAME[U['next']]])

C.build('BD03_002', [])                            # nothing pressed: its first window runs out
cpu = C.boot()
played = C.run(cpu, 2)
C.check('no press: the death scene, with no beep', (played[1], [b[2] for b in cpu.beeps]), ('BD03_002_510', []))

R = C.NAMES['BD02_003_main']                     # a route: Up, Down, Left are ways, Right is not offered
r = R['decisions'][0]
g = R['fields'][r['frame']]
ways = {k: C.NAME[o] for k, o in zip(['none', 'up', 'down', 'left', 'right', 'fire'], r['outcomes']) if o is not None}
for first, then, want, sounds in [('up', 'down', ways['down'], [0, 0]), ('up', 'right', None, [0, 1])]:
    C.build('BD02_003', [f'BD02_003_main:{g + 5}:{first}', f'BD02_003_main:{g + 25}:{then}'])
    cpu = C.boot()
    played = C.run(cpu, 2)
    nxt = want or (R['next'] and C.NAME[R['next']])
    C.check(f'route: {first} then {then}: the last move wins ({nxt})', (played[1], [b[2] for b in cpu.beeps]),
            (nxt, sounds))
sys.exit(1 if C.failed else 0)

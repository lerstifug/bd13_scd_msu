"""Run the real player ROM in a small 65816 interpreter:  python sim_player.py [ALLCHEAT [FROM=clip]] [--start-at field] [--units n] [--fields n]

Only the CPU is real: the MSU-1 is the real build/game/bd13msu.msu (its data port; a DMA just skips bytes), the PPU
and timers are stubs that give one pass per field, and the joypad is idle (--start-at: START held in that field).  So this checks the game logic and the
test presses, fast and with no window: it prints the units played and compares them with the cheat map's expected
order.  Picture and sound are not checked here (verify_bsnes.py, bulk.py).
"""
import json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = os.path.join(HERE, '..', 'build', 'game')


class Done(Exception):
    pass


class CPU:
    def __init__(self, rom, msu):
        self.rom, self.wram, self.msu = rom, bytearray(0x2000), msu
        self.a = self.x = self.y = self.d = 0
        self.s, self.m, self.xf, self.e = 0x1FF, 1, 1, 1
        self.n = self.v = self.z = self.c = 0
        self.pc = rom[0x7FFC] | rom[0x7FFD] << 8
        self.seek = bytearray(4)
        self.line, self.vtimer, self.hi = 0, 0, 0
        self.dma_a, self.dma_n = 0, 0
        self.track, self.tracks, self.fields, self.count = 0, [], 0, 0
        self.press, self.oam = set(), 0
        self.sram = bytearray(0x800)                                # cartridge RAM ($70:0000)
        self.dma_b, self.vaddr, self.bg1map = 0, 0, bytes(2048)     # the map a pause reads
        self.obj, self.tm, self.oam_data, self.tiles = 0, 3, None, {}
        self.pauses, self.tm_log = [], []
        self.dma_bank, self.pad = 0, None                           # pad: (the $4218, $4219 bits held) or None
        self.apu, self.apu_addr, self.apu_run, self.beeps = [None] * 4, 0, False, []   # the sound chip: see rd / wr

    # ---- memory (bank 0 only) --------------------------------------------------------------------------------
    def rd(self, a):
        a &= 0xFFFF
        if a < 0x2000:
            return self.wram[a]
        if a >= 0x8000:
            return self.rom[a - 0x8000]
        if a == 0x2000:
            return 0                                                # not busy
        if a == 0x2001:
            return self.msu.read(1)[0]
        if a == 0x213D:                                             # the scanline, low then high
            self.hi ^= 1
            return self.line & 255 if self.hi else self.line >> 8
        if a == 0x4219 and self.fields in self.press:
            return 0x10                                             # start, held for that field
        if a in (0x4218, 0x4219) and self.pad:
            return self.pad[a - 0x4218]
        if 0x2140 <= a <= 0x2143:                                   # the sound chip boot ROM: ready ($BBAA), then it
            if self.apu[0] is None:                                 # echoes port 0 (the upload handshake)
                return {0x2140: 0xAA, 0x2141: 0xBB}.get(a, 0)
            return self.apu[0] if a == 0x2140 else 0
        if a == 0x2137:
            self.line = (self.line + 50) % 262                      # (time passes)
            self.hi = 0
        if a == 0x213F:
            self.hi = 0
        return 0

    def wr(self, a, v):
        a &= 0xFFFF
        if a < 0x2000:
            self.wram[a] = v
        elif 0x2140 <= a <= 0x2143:
            if a == 0x2142:
                self.apu_addr += 1                                  # a block address: the image, then the start
            if a == 0x2140 and self.apu_run:
                self.beeps.append((self.fields, self.rd16(0x12), self.apu[1], self.rd16(0x16)))   # a beep: field, unit,
                # 0 good 1 bad, the unit field (PK)
            elif a == 0x2140 and self.apu_addr == 2:
                self.apu_run = True                                 # (the start command: the driver runs)
            self.apu[a - 0x2140] = v
        elif 0x2000 <= a <= 0x2003:
            self.seek[a - 0x2000] = v
            if a == 0x2003:                                         # a new unit (UNIT is set by now)
                self.msu.seek(int.from_bytes(self.seek, 'little'))
                self.tracks.append(self.rd16(0x12) + 1)
        elif a == 0x2007 and v == 0 and self.rd(0x13) | self.rd(0x12):
            self.pauses.append((self.fields, self.rd(0x00), self.rd16(0x16), self.rd16(0x12)))   # sound off: flags, field, unit
        elif a == 0x2101:
            self.obj = v
        elif a == 0x212C:
            self.tm = v
            self.tm_log.append((self.fields, v))
        elif a == 0x2116:
            self.vaddr = self.vaddr & 0xFF00 | v
        elif a == 0x2117:
            self.vaddr = self.vaddr & 0xFF | v << 8
        elif a == 0x4301:
            self.dma_b = v
        elif a == 0x4304:
            self.dma_bank = v
        elif a == 0x4209:
            self.vtimer = self.vtimer & 0xFF00 | v
        elif a == 0x420A:
            self.vtimer = self.vtimer & 0xFF | v << 8
        elif a == 0x4305:
            self.dma_n = self.dma_n & 0xFF00 | v
        elif a == 0x4306:
            self.dma_n = self.dma_n & 0xFF | v << 8
        elif a == 0x4302:
            self.dma_a = self.dma_a & 0xFF00 | v
        elif a == 0x4303:
            self.dma_a = self.dma_a & 0xFF | v << 8
        elif a == 0x420B and v:
            n = self.dma_n or 0x10000
            if self.dma_a == 0x2001:
                self.msu.seek(n, 1)                                 # the DMA from the data port
            elif self.dma_b == 0x39:                                # VRAM to work RAM: BG1's map
                self.wram[self.dma_a:self.dma_a + n] = self.bg1map[:n]
            elif self.dma_b == 0x04:
                self.oam = self.dma_bank << 16 | self.dma_a         # (a menu's outline, the PAUSE text)
                self.oam_data = bytes(self.rd_long(self.oam + k) for k in range(n))
            elif self.dma_b == 0x18:
                self.tiles[self.vaddr] = bytes(self.rd(self.dma_a + k) for k in range(n))
        elif a == 0x2100 and v == 0x80:
            self.fields += 1

    def rd_long(self, a):
        if a >> 16 == 0x70:
            return self.sram[a & 0x7FF]
        if a >> 16 == 1 and a & 0x8000:
            return self.rom[0x8000 + (a & 0x7FFF)]                  # ROM bank 1 (press lists, the cheat menu's boxes)
        return self.rd(a)

    def wr_long(self, a, v):
        if a >> 16 == 0x70:
            self.sram[a & 0x7FF] = v
        else:
            self.wr(a, v)

    def rd16(self, a):
        return self.rd(a) | self.rd(a + 1) << 8

    def fetch(self):
        v = self.rom[(self.pc & 0x7FFF)]
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        return self.fetch() | self.fetch() << 8

    def push(self, v):
        self.wr(self.s, v)
        self.s = (self.s - 1) & 0xFFFF

    def pull(self):
        self.s = (self.s + 1) & 0xFFFF
        return self.rd(self.s)

    def nz(self, v, wide):
        self.z = (v == 0)
        self.n = (v >> (15 if wide else 7)) & 1
        return v

    # ---- one instruction -------------------------------------------------------------------------------------
    def addr(self, mode):
        if mode == 'dp':
            return (self.d + self.fetch()) & 0xFFFF
        if mode == 'dpx':
            return (self.d + self.fetch() + self.x) & 0xFFFF
        if mode == 'abs':
            return self.fetch16()
        if mode == 'absx':
            return (self.fetch16() + self.x) & 0xFFFF
        if mode == 'absy':
            return (self.fetch16() + self.y) & 0xFFFF
        raise ValueError(mode)

    def load(self, mode, wide):
        if mode == 'imm':
            return self.fetch16() if wide else self.fetch()
        a = self.addr(mode)
        return self.rd16(a) if wide else self.rd(a)

    def store(self, mode, v, wide):
        a = self.addr(mode)
        self.wr(a, v & 255)
        if wide:
            self.wr(a + 1, v >> 8 & 255)

    def getA(self):
        return self.a if not self.m else self.a & 255

    def setA(self, v):
        self.a = v & 0xFFFF if not self.m else (self.a & 0xFF00) | (v & 255)

    def cmp(self, r, v, wide):
        self.c = r >= v
        self.nz((r - v) & (0xFFFF if wide else 0xFF), wide)

    def branch(self, cond):
        o = self.fetch()
        if cond:
            self.pc = (self.pc + (o - 256 if o & 128 else o)) & 0xFFFF

    def step(self):
        pc0 = self.pc
        op = self.fetch()
        self.count += 1
        am, xw = not self.m, not self.xf
        mask = 0xFFFF if am else 0xFF
        LDA = {0xA9: 'imm', 0xA5: 'dp', 0xB5: 'dpx', 0xAD: 'abs', 0xBD: 'absx', 0xB9: 'absy'}
        STA = {0x85: 'dp', 0x95: 'dpx', 0x8D: 'abs', 0x9D: 'absx', 0x99: 'absy'}
        AND = {0x29: 'imm', 0x25: 'dp', 0x2D: 'abs', 0x3D: 'absx', 0x39: 'absy'}
        ORA = {0x09: 'imm', 0x05: 'dp', 0x15: 'dpx', 0x0D: 'abs', 0x1D: 'absx', 0x19: 'absy'}
        EOR = {0x49: 'imm', 0x45: 'dp', 0x4D: 'abs'}
        ADC = {0x69: 'imm', 0x65: 'dp', 0x6D: 'abs', 0x7D: 'absx', 0x79: 'absy'}
        SBC = {0xE9: 'imm', 0xE5: 'dp', 0xED: 'abs', 0xFD: 'absx', 0xF9: 'absy'}
        CMP = {0xC9: 'imm', 0xC5: 'dp', 0xCD: 'abs', 0xDD: 'absx', 0xD9: 'absy'}
        if op in (0xAF, 0xBF, 0x8F, 0x9F):                          # long addressing (the saved game)
            a = self.fetch16() | self.fetch() << 16
            if op in (0xBF, 0x9F):
                a += self.x
            if op in (0xAF, 0xBF):
                v = self.rd_long(a) | (self.rd_long(a + 1) << 8 if am else 0)
                self.setA(self.nz(v, am))
            else:
                self.wr_long(a, self.a & 255)
                if am:
                    self.wr_long(a + 1, self.a >> 8)
        elif op in LDA:
            self.setA(self.nz(self.load(LDA[op], am), am))
        elif op in STA:
            self.store(STA[op], self.a, am)
        elif op in AND:
            self.setA(self.nz(self.getA() & self.load(AND[op], am), am))
        elif op in ORA:
            self.setA(self.nz(self.getA() | self.load(ORA[op], am), am))
        elif op in EOR:
            self.setA(self.nz(self.getA() ^ self.load(EOR[op], am), am))
        elif op in ADC:
            r = self.getA() + self.load(ADC[op], am) + int(self.c)
            self.c = r > mask
            self.setA(self.nz(r & mask, am))
        elif op in SBC:
            r = self.getA() - self.load(SBC[op], am) - (1 - int(self.c))
            self.c = r >= 0
            self.setA(self.nz(r & mask, am))
        elif op in CMP:
            self.cmp(self.getA(), self.load(CMP[op], am), am)
        elif op in (0xA2, 0xA6, 0xAE, 0xBE):
            self.x = self.nz(self.load({0xA2: 'imm', 0xA6: 'dp', 0xAE: 'abs', 0xBE: 'absy'}[op], xw), xw)
        elif op in (0xA0, 0xA4, 0xAC, 0xBC):
            self.y = self.nz(self.load({0xA0: 'imm', 0xA4: 'dp', 0xAC: 'abs', 0xBC: 'absx'}[op], xw), xw)
        elif op in (0x86, 0x8E):
            self.store({0x86: 'dp', 0x8E: 'abs'}[op], self.x, xw)
        elif op in (0x84, 0x8C):
            self.store({0x84: 'dp', 0x8C: 'abs'}[op], self.y, xw)
        elif op in (0x64, 0x74, 0x9C, 0x9E):
            self.store({0x64: 'dp', 0x74: 'dpx', 0x9C: 'abs', 0x9E: 'absx'}[op], 0, am)
        elif op in (0xE0, 0xE4, 0xEC):
            self.cmp(self.x, self.load({0xE0: 'imm', 0xE4: 'dp', 0xEC: 'abs'}[op], xw), xw)
        elif op in (0xC0, 0xC4, 0xCC):
            self.cmp(self.y, self.load({0xC0: 'imm', 0xC4: 'dp', 0xCC: 'abs'}[op], xw), xw)
        elif op == 0xE8:
            self.x = self.nz((self.x + 1) & (0xFFFF if xw else 0xFF), xw)
        elif op == 0xCA:
            self.x = self.nz((self.x - 1) & (0xFFFF if xw else 0xFF), xw)
        elif op == 0xC8:
            self.y = self.nz((self.y + 1) & (0xFFFF if xw else 0xFF), xw)
        elif op == 0x88:
            self.y = self.nz((self.y - 1) & (0xFFFF if xw else 0xFF), xw)
        elif op in (0x1A, 0x3A):                                    # INC A, DEC A
            self.setA(self.nz((self.getA() + (1 if op == 0x1A else -1)) & (0xFFFF if am else 0xFF), am))
        elif op in (0xE6, 0xC6):
            a = (self.d + self.fetch()) & 0xFFFF
            v = self.rd16(a) if am else self.rd(a)
            v = self.nz((v + (1 if op == 0xE6 else -1)) & mask, am)
            self.wr(a, v & 255)
            if am:
                self.wr(a + 1, v >> 8)
        elif op == 0x0A:
            v = self.getA()
            self.c = (v >> (15 if am else 7)) & 1
            self.setA(self.nz((v << 1) & mask, am))
        elif op == 0x4A:
            v = self.getA()
            self.c = v & 1
            self.setA(self.nz(v >> 1, am))
        elif op == 0xAA:
            self.x = self.nz(self.a if xw and am else self.a & (0xFFFF if xw else 0xFF), xw)
        elif op == 0xA8:
            self.y = self.nz(self.a if xw else self.a & 255, xw)
        elif op == 0x8A:
            self.setA(self.nz(self.x & mask, am))
        elif op == 0x98:
            self.setA(self.nz(self.y & mask, am))
        elif op == 0xEB:                                            # xba
            self.a = (self.a >> 8 | self.a << 8) & 0xFFFF
            self.nz(self.a & 255, False)
        elif op == 0xBB:
            self.x = self.nz(self.y, xw)
        elif op == 0x9B:
            self.y = self.nz(self.x, xw)
        elif op == 0x9A:
            self.s = self.x
        elif op == 0x5B:
            self.d = self.a
        elif op == 0x48:
            if am:
                self.push(self.a >> 8)
            self.push(self.a & 255)
        elif op == 0x68:
            v = self.pull()
            if am:
                v |= self.pull() << 8
            self.setA(self.nz(v, am))
        elif op == 0xDA:
            self.push(self.x >> 8)
            self.push(self.x & 255)
        elif op == 0xFA:
            v = self.pull()
            self.x = self.nz(v | self.pull() << 8, True)
        elif op == 0x5A:
            self.push(self.y >> 8)
            self.push(self.y & 255)
        elif op == 0x7A:
            v = self.pull()
            self.y = self.nz(v | self.pull() << 8, True)
        elif op == 0xAB:
            self.pull()
        elif op == 0x2C:
            v = self.rd(self.fetch16())
            self.n, self.v = v >> 7, (v >> 6) & 1
            self.z = (self.getA() & v) == 0
        elif op == 0x18:
            self.c = 0
        elif op == 0x38:
            self.c = 1
        elif op == 0x78:
            pass
        elif op == 0xFB:
            self.c, self.e = self.e, self.c
        elif op == 0xC2 or op == 0xE2:
            b = self.fetch()
            if b & 0x20:
                self.m = 1 if op == 0xE2 else 0
            if b & 0x10:
                self.xf = 1 if op == 0xE2 else 0
            if b & 0x01:
                self.c = 1 if op == 0xE2 else 0
        elif op == 0xCB:                                            # wai: the timer
            self.line = self.vtimer
        elif op == 0x10:
            self.branch(not self.n)
        elif op == 0x30:
            self.branch(self.n)
        elif op == 0x70:
            self.branch(self.v)
        elif op == 0x50:
            self.branch(not self.v)
        elif op == 0x90:
            self.branch(not self.c)
        elif op == 0xB0:
            self.branch(self.c)
        elif op == 0xF0:
            self.branch(self.z)
        elif op == 0xD0:
            self.branch(not self.z)
        elif op == 0x80:
            self.branch(True)
        elif op == 0x4C:
            self.pc = self.fetch16()
        elif op == 0x20:
            t = self.fetch16()
            r = (self.pc - 1) & 0xFFFF
            self.push(r >> 8)
            self.push(r & 255)
            self.pc = t
        elif op == 0x60:
            v = self.pull()
            self.pc = ((v | self.pull() << 8) + 1) & 0xFFFF
        else:
            raise ValueError(f'opcode ${op:02X} at ${pc0:04X}')


def main():
    args = sys.argv[1:]
    cheat = [a for a in args if a.isalpha()]
    if cheat:
        subprocess.run([sys.executable, 'pack_game.py', f'CHEAT={cheat[0]}', '--no-msu', '--no-pcm']
                       + [a for a in args if a.startswith('FROM=')], check=True, cwd=HERE, stdout=subprocess.DEVNULL)
    names = dict((int(n), s) for n, s in re.findall(r'; (\d+) (BD\S+|TITLE|SAVEMENU|CHEATMENU)', open(os.path.join(HERE, 'units.inc')).read()))
    rom = open(os.path.join(GAME, 'bd13msu.sfc'), 'rb').read()
    msu = open(os.path.join(GAME, 'bd13msu.msu'), 'rb', buffering=1 << 20)
    exp = json.load(open(os.path.join(GAME, 'expected.json')))['order'] if cheat else None
    want = [o for o in exp if not o.startswith('(')] if exp else None
    cpu = CPU(rom, msu)
    if '--start-at' in args:
        cpu.press = {int(args[args.index('--start-at') + 1])}
    units = int(args[args.index('--units') + 1]) if '--units' in args else 40
    limit = len(want) + 2 if want else units
    n, random = 0, 0
    sys.path.insert(0, HERE)
    import pack_game
    kinds = {u['name']: u['kind'] for u in pack_game.load_units()}
    oam, stop = 0, int(args[args.index('--fields') + 1]) if '--fields' in args else 400000
    while len(cpu.tracks) <= limit and cpu.fields < stop:
        cpu.step()
        if cpu.oam != oam:
            oam = cpu.oam
            print(f'     outline: OAM image ${oam:04X} at field {cpu.fields}', flush=True)
        if len(cpu.tracks) > n:                                     # a new unit: print the state it was entered from
            n = len(cpu.tracks)
            u = cpu.tracks[-1] - 1
            s = cpu.rd16(0x20)
            p = 0x10000 | s                                         # (the press lists are in bank 1)
            print(f'{n - 1:4d} {names[u]:16s} field {cpu.fields:6d}  script ${s:04X}: unit '
                  f'{cpu.rd_long(p) | cpu.rd_long(p + 1) << 8} field {cpu.rd_long(p + 2) | cpu.rd_long(p + 3) << 8} '
                  f'moves {cpu.rd_long(p + 4)}', flush=True)
            if want and n <= len(want) and names[u] != want[n - 1]:
                free = ('deathfile', 'revive')              # (a random list: any of its deaths, then that one's revive)
                if kinds[names[u]] in free and kinds[want[n - 1]] in free:
                    random += 1
                else:
                    print(f'DIVERGED: expected {want[n - 1]}')
                    sys.exit(1)
            if want and n == len(want):
                print(f'all {n} units as expected ({random} random deaths or revives), {cpu.fields} fields, '
                      f'{cpu.count} instructions')
                return


if __name__ == '__main__':
    main()

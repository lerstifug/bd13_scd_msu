; APP.BIN - sub 68000 program of the Brain Dead 13 Sega CD player (two-layer frames, shadow cells, scrolling scenes,
; game logic; stream format version 5, see encode_v3.py and encode_set.py).
; The loader (Chilly Willy cdboot) reads this from the disc root into sub PRG RAM at $8000 and calls it with
; a6 = the loader global variables (vector table: +0 vblank handler, +12 ReadCD, +36 LoadFile).
;
; The disc holds one stream file per unit (the main path of a clip, or one of its death scenes) and the logic table
; LOGIC.BIN, loaded at boot (see build_disc.py: per unit its sectors, what follows it, its decisions).
;
; Main thread:  load MAIN.BIN into Word RAM, set up the PCM chip, install the interrupt handler, hand Word RAM to
;               the main CPU, then loop: switch units when the handler asks (do_switch), and keep the 256 KB ring
;               full by streaming the file of the current unit sector by sector (pump).
; Handler:      the main CPU interrupts us once per vertical blank and passes the controller in comm word 0.
;               START pauses (picture held, sound stopped, the 3DO PAUSE overlay as sprites); START again restarts
;               the clip file from its start, A/B/C opens the save menu instead (see pausectl).
;               Otherwise, when the PCM chip has started playing the audio of the next frame (video follows the
;               audio clock) and that frame is fully buffered, we "render" it into Word RAM: both complete plane
;               maps (the scene base maps with the cells of this frame patched in), the tiles of the frame, and on a scene
;               change the palette and scene tiles; the main CPU then only DMAs them to the video chip.  After each
;               frame the decisions of the unit are checked (a wrong move starts the death scene); at the end of a unit
;               the next one starts (route chosen, JUMP, the revive clip after a death, back to the clip after it).
;
; Scrolling scenes: both planes scroll vertically; each frame may bring plane rows coming into view (map words
; into the base maps, tiles into the block of ROWK scene tiles of that row).
; Tile references: a frame tile slot is new data, a copy of a tile in the pool of the previous frame, or the same
; tile as an earlier slot of the frame (shares its VRAM tile).
;
; Word RAM (sub $080000 / main $200000): see the WR_ offsets below.  Status 0 nothing, 1 frame, 2 scene + frame.

LOAD_FILE       equ     36
VBLANK_HANDLER  equ     0

CHUNKBYTES      equ     65536
RING            equ     $20000
LOGIC           equ     $60000          ; LOGIC.BIN (build_disc.py), loaded at boot: above the ring
TITLEB          equ     $64000          ; TITLE.BIN: the start screen buttons and outline sprites (build_disc.py)
CHEATB          equ     $68000          ; CHEATS.BIN: the cheat menu entries, box sprites and press lists (build_disc.py)
BRAMWORK        equ     $66000          ; backup RAM: the BIOS scratch area ($640 bytes) ...
BRAMSTR         equ     $66640          ; ... its display strings (12 bytes)
SAVEBUF         equ     $66700          ; the saved game: the text BD13, build id, clip, visited units (64 bytes)
_BURAM          equ     $5F16
RINGSIZE        equ     $40000
RINGMASK        equ     $3FFFF
AUD_LEAD        equ     6               ; frames of audio queued ahead (frame f record carries frame f+6 audio)
PREROLL         equ     AUD_LEAD*1200   ; audio of the first frames of a scene, at the end of each scene record

ROWK            equ     33              ; scrolling scenes: scene tiles per plane row

; buttons in comm word 0 (written by the main CPU every vertical blank): 1 = pressed
B_ACTION        equ     4               ; bits 0-3: up, down, left, right; 4: fire (A, B or C)
B_START         equ     7

; the 3DO button sounds (beeps.py): BEEPS.BIN is copied into PCM wave RAM once at boot, played on channel 1
        include "build/BEEPS.INC"
FLASH_GOOD      equ     $00E0           ; DBGINPUT: backdrop green for one frame after an accepted press,
FLASH_BAD       equ     $000E           ; red after one that is not (wrong move, not offered, outside a window)
BLINK_TIME      equ     30              ; the PAUSE text: half a second on, half a second off
FLASH_TIME      equ     6               ; vertical blanks counted down from the press: 5 shown = one 12 fps frame

; logic table (LOGIC.BIN, a disc file loaded at LOGIC): header u16 units, start unit, 0, save menu unit; units of
; 16 bytes:
U_LBA           equ     0               ; long
U_NSECT         equ     4
U_NEXT          equ     6               ; $FFFF = none
U_RESTART       equ     8               ; death scenes: the clip to restart after the revive clip
U_DECOFF        equ     10              ; decisions: offset from the table start
U_NDEC          equ     12
U_FLAGS         equ     14              ; bit 0 a revive clip, 1 the save menu, 2 a death clip of its own (after
                                        ; it: its revive clip, then back to the clip where the way was chosen),
                                        ; 3 COND rules follow the decisions of the unit
; decisions of 20 bytes: u16 frame, window, kind (0 death, 1 route), 6 outcomes (unit / $FFFF), 0
D_FRAME         equ     0
D_WINDOW        equ     2
D_KIND          equ     4
D_OUT           equ     6               ; 6 outcomes: no input, up, down, left, right, fire
D_CUT           equ     18              ; route: the frame where the chosen route takes over

WR              equ     $080000
WR_STATUS       equ     0
WR_KF           equ     2               ; frame tiles
WR_POOL         equ     4               ; their VRAM pool (tile number)
WR_KS           equ     6               ; scene tiles (scene change only)
WR_VS           equ     8               ; vertical scroll of both planes
WR_MASK         equ     $A              ; border sprites: line << 8 | black colour, 0 = none (scene change only)
WR_NRUN         equ     $C              ; new plane rows: runs of scene tiles
WR_FTO          equ     $E              ; offset of the tiles of this frame (FT0 or FT1)
WR_PAL          equ     $10             ; 64 words
WR_RUNS         equ     $1110           ; up to 32 x (first scene tile, count): a whole plane of new rows
WR_R1           equ     $B0             ; last scene tile in VRAM tiles 1.. (depends on the pool size)
WR_MAPA         equ     $100            ; plane A map, 32 x 32 words
WR_MAPB         equ     $900            ; plane B map
WR_DBG          equ     $1100           ; word: backdrop colour showing the buffer level (see handler)
WR_FT0          equ     $1200           ; frame pool tiles, alternating buffers (up to 560 x 32 bytes each)
WR_FT1          equ     $5800
WR_RT           equ     $15600          ; the tiles of the new rows (up to 32 x ROWK; above the largest WR_ST)
WR_ST           equ     $B400           ; scene tiles (up to 1287 x 32 bytes, with 300-tile frame pools)
WR_WMAPA        equ     $18000          ; menu screens (H40): plane A map, 64 x 32 words
WR_WMAPB        equ     $19000          ; ... plane B
WR_X            equ     $1E000          ; status 3 (overlay): what the main CPU does, X_F flags:
X_F             equ     0               ;   XF_SPR: DMA X_N tiles to VRAM tile X_V, write X_S sprites (8 bytes
X_N             equ     2               ;   each, from X_SPR) as sprites 16.. after the border sprites;
X_V             equ     4               ;   XF_PAL: upload WR_PAL to CRAM; XF_REBOOT: restart the console
X_S             equ     6
X_SPR           equ     8
X_TILES         equ     $100
XF_SPR          equ     1
XF_PAL          equ     2
XF_REBOOT       equ     4

V_WR            equ     $A000           ; long: bytes of the current file read from the disc so far
V_VP            equ     $A004           ; long: file offset of the next record for the video side
V_SEC           equ     $A00C           ; word: next sector of the file to take from the drive
V_CHUNK         equ     $A00E           ; word: next PCM ring chunk (0..11) to write
V_STARTED       equ     $A010           ; word: PCM channel started?
V_FCH           equ     $A012           ; word: PCM chunk holding the audio of the next frame to show
V_PAR           equ     $A014           ; word: frame pool parity
V_WAIT          equ     $A016           ; word: vblanks we have answered "not yet" in a row
V_GV            equ     $A018           ; long: loader global variables (a6)
V_RUN           equ     $A01C           ; word: the drive is streaming (a ROMREADN is active)
HDRBUF          equ     $A020           ; 8 bytes
RPARAM          equ     $A030           ; ROMREADN parameters: start sector, sector count
RHDR            equ     $A038           ; sector header from CDCTRN
V_O             equ     $A040           ; long: stream offset being read in do_frame
V_NC            equ     $A044           ; word: cells in this frame
V_NT            equ     $A046           ; word: tile slots in this frame
V_NR            equ     $A048           ; word: new plane rows in this frame
V_FB            equ     $A04A           ; word: bytes of tile-slot flag words
V_FTC           equ     $A04C           ; long: the tile buffer of this frame (sub address)
V_FTP           equ     $A050           ; long: the previous frame
V_POOL0         equ     $A054           ; word: first tile of frame pool 0
V_POOL1         equ     $A056           ; word: ... of pool 1
V_UNIT          equ     $A058           ; word: unit playing
V_LBA           equ     $A05A           ; long: its first sector
V_NSECT         equ     $A05E           ; word: its sectors
V_REQ           equ     $A060           ; word: unit + 1 to switch to (0 = none); cleared when the switch is done
V_REQOFF        equ     $A062           ; long: ... starting at this file offset (a scene record)
V_REQFR         equ     $A066           ; word: ... which is this frame of the unit
V_FRAME         equ     $A068           ; word: unit frame of the next frame to show
V_FRESH         equ     $A070           ; word: playback (re)starts at the next scene record: use its preroll
V_ROUTE         equ     $A072           ; word: route chosen in this unit ($FFFF none)
V_RETURN        equ     $A074           ; word: unit to go back to after the revive clip
V_DECI          equ     $A076           ; word: next decision of the unit to settle
V_PADP          equ     $A078           ; word: buttons last vertical blank
V_PRESS         equ     $A07A           ; word: buttons pressed (edges) and not used yet
V_PAUSED        equ     $A07C           ; word: 0 playing, 1 pause overlay, 2 save menu
V_EOF           equ     $A07E           ; word: the whole file has been read
V_SCRP          equ     $A080           ; long: next entry of the test script
V_PWAIT         equ     $A084           ; word: vertical blanks since the last scripted press while paused
V_UPTR          equ     $A086           ; long: the entry of the unit in the logic table
V_CUT           equ     $A08A           ; word: switch frame of the route chosen
V_CHOFF         equ     $A08C           ; word: PCM channel on/off register value (bit n = 1: channel n off)
V_FLASH         equ     $A08E           ; word: DBGINPUT backdrop colour
V_FLASHT        equ     $A090           ; word: vertical blanks until the backdrop goes back to black
V_RESUME        equ     $A092           ; word: the clip to play when the game resumes from a pause
V_MSEL          equ     $A094           ; word: save menu: the selected button
V_MSHOWN        equ     $A096           ; word: save menu: on screen (its frame has been shown)
V_RAND          equ     $A098           ; word: random number state
V_VBL           equ     $A09A           ; word: vertical blanks counted
V_VISIT         equ     $A0A0           ; 64 bytes: a bit per unit that has been played (COND rules, saves)
V_PRE           equ     $A09C           ; word: unit + 1 being read ahead into the ring after this one (0 = none)
V_NBASE         equ     $A0E0           ; long: its stream position in the ring (its file offset 0)
V_WIDE          equ     $A0E4           ; word: a menu screen (H40) is showing
V_TSEL          equ     $A0E6           ; word: start screen: the selected button
V_TSHOWN        equ     $A0E8           ; word: start screen: on screen
V_SAVEOK        equ     $A0EA           ; word: SAVEBUF holds a saved game of this disc
V_SAVEREQ       equ     $A0EC           ; word: write SAVEBUF to backup RAM (the main thread does it)
V_BRAM          equ     $A0EE           ; word: backup RAM is usable
V_BLINK         equ     $A0F0           ; word: vertical blanks since the PAUSE text last blinked
V_BLINKON       equ     $A0F2           ; word: the PAUSE text is showing
V_CODE          equ     $A0F4           ; word: start screen: the cheat code entered so far
V_CSEL          equ     $A0F6           ; word: cheat menu: the selected entry
V_CSHOWN        equ     $A0F8           ; word: cheat menu: on screen
AUDBUF          equ     $A100           ; 1200 bytes
V_CHEAT         equ     $A600           ; word: a cheat map is playing (its press list is the test script)
V_RINGI         equ     $A602           ; word: the newest entry of V_URING (its offset)
V_RSCRP         equ     $A604           ; long: where the press list goes back to when a pause resumes
V_DBGP          equ     $A608           ; word: DBGPAUSE test: 0 waiting, 1 paused by it, 2 resumed by it
V_DBGW          equ     $A60A           ; word: DBGPAUSE test: vertical blanks paused
V_URING         equ     $A610           ; 128 bytes: the last 16 units started, each: unit, 0, the press list there (long)
BASEA           equ     $B000           ; base map, plane A: 32 rows x 32 words (BASEB must follow it)
BASEB           equ     $B800           ; base map, plane B
TMPMAP          equ     $C000           ; up to 32 x 28 words
ENTBUF          equ     $C800           ; frame entries + mode words (up to 672 + 42 words)
REFBUF          equ     $CE00           ; tile-slot flag words + reference words (up to 84 + 1344 words)
ROWBUF          equ     $F000           ; new plane rows (116 bytes each, up to 32: a camera jump of a screen)
SLOTT           equ     $DE00           ; VRAM tile of each tile slot (up to 1344 words)

        org     $8000

start:
        move.l  a6,V_GV
        lea     name_main(pc),a0
        lea     WR,a1
        movea.l LOAD_FILE(a6),a2
        jsr     (a2)
        bne     loadfail
        movea.l V_GV,a6
        lea     name_logic(pc),a0       ; the game logic
        lea     LOGIC,a1
        movea.l LOAD_FILE(a6),a2
        jsr     (a2)
        bne     loadfail
        movea.l V_GV,a6
        lea     name_title(pc),a0       ; the start screen
        lea     TITLEB,a1
        movea.l LOAD_FILE(a6),a2
        jsr     (a2)
        bne     loadfail
        movea.l V_GV,a6
        lea     name_cheats(pc),a0      ; the cheat menu
        lea     CHEATB,a1
        movea.l LOAD_FILE(a6),a2
        jsr     (a2)
        bne     loadfail
        movea.l V_GV,a6
        lea     name_beeps(pc),a0       ; the button sounds, into the stream ring (not in use yet)
        lea     RING,a1
        movea.l LOAD_FILE(a6),a2
        jsr     (a2)
        bne     loadfail

        clr.w   V_PAR
        clr.w   V_RUN
        move.w  #1,V_EOF                ; nothing to read until the first switch
        clr.w   V_PADP
        clr.w   V_PRESS
        clr.w   V_PAUSED
        clr.w   V_PWAIT
        move.w  #$FFFF,V_RETURN
        lea     script(pc),a0
        move.l  a0,V_SCRP
        clr.w   V_CHEAT
        clr.w   V_RINGI
        clr.w   V_DBGP
        clr.w   V_DBGW
        lea     V_URING,a0              ; (no unit in the ring yet)
        moveq   #31,d0
.cr:    move.l  #$FFFF0000,(a0)+
        dbra    d0,.cr
        clr.w   V_FLASH
        clr.w   V_FLASHT
        clr.w   V_WIDE
        clr.w   V_TSHOWN
        clr.w   V_CSHOWN
        clr.w   V_CODE
        clr.w   V_SAVEREQ
        bsr     bram_load
        lea     V_VISIT,a0              ; nothing visited yet
        moveq   #15,d0
.cv:    clr.l   (a0)+
        dbra    d0,.cv
        bsr     pcm_init
        lea     RING,a0                 ; beeps into wave RAM, once: they stay there for the whole game
        move.w  #BEEP_BASE,d0
        move.w  #BEEP_SIZE,d1
        bsr     pcm_copy
        move.w  LOGIC+2,d0          ; the start unit
        bsr     request

        movea.l V_GV,a6
        lea     handler(pc),a0
        move.l  a0,VBLANK_HANDLER(a6)
        move.b  #'M',$FFFF800F.w        ; sub comm port: ready
        bset    #0,$FFFF8003.w          ; RET: hand Word RAM to the main CPU

reader:
        tst.w   V_REQ
        beq.s   .p
        bsr     do_switch
.p:     tst.w   V_SAVEREQ
        beq.s   .q
        bsr     bram_save
.q:     bsr     pump
        bra.s   reader

loadfail:
        move.b  #'E',$FFFF800F.w
.hang:  bra.s   .hang

; ---- play unit d0.w from its start.  When it is the unit the reader is already streaming in after this one (read
; ahead, see pump), switch at once: no drive stop, no seek (the 3DO likewise starts seeking at its JUMP, 25 frames
; early).  Otherwise ask the main thread to switch (do_switch).
request:
        move.w  V_PRE,d1
        subq.w  #1,d1
        cmp.w   d0,d1
        bne     .hard
        move.w  d0,V_UNIT               ; read ahead: its data follows in the ring
        bsr     ring_push
        move.w  d0,d1
        lsr.w   #3,d1
        lea     V_VISIT,a0
        bset    d0,0(a0,d1.w)
        lsl.w   #4,d0
        lea     LOGIC+8,a0
        adda.w  d0,a0
        move.l  a0,V_UPTR
        move.l  V_NBASE,d0
        addi.l  #16,d0                  ; the first record follows the 16-byte stream header
        move.l  d0,V_VP
        clr.w   V_PRE
        bset    #0,V_CHOFF+1            ; audio of this clip off until the preroll of the new scene
        move.b  V_CHOFF+1,$FF0011
        clr.w   V_STARTED
        clr.w   V_WAIT
        clr.w   V_FCH
        clr.w   V_CHUNK
        move.w  #1,V_FRESH
        clr.w   V_FRAME
        clr.w   V_DECI
        clr.w   V_PRESS
        move.w  #$FFFF,V_ROUTE
        rts
.hard:  move.l  #16,V_REQOFF            ; the first record follows the 16-byte stream header
        clr.w   V_REQFR
        move.w  #$FFFF,V_ROUTE          ; a new unit: no route chosen yet
        addq.w  #1,d0
        move.w  d0,V_REQ                ; last: the handler waits while V_REQ is set
        rts

; ---- d0.w = a unit starting: into the ring of units started, with where the press list is.  Keeps all registers.
ring_push:
        movem.l d1/a0,-(sp)
        move.w  V_RINGI,d1
        addq.w  #8,d1
        andi.w  #$78,d1
        move.w  d1,V_RINGI
        lea     V_URING,a0
        move.w  d0,0(a0,d1.w)
        move.l  V_SCRP,4(a0,d1.w)
        movem.l (sp)+,d1/a0
        rts

; ---- a pause: V_RSCRP = where the press list goes back to when it resumes.  In a cheat map the clip V_RESUME plays
; again, so its presses must too: where the list was when that unit last started (newest first).  Else, or when it
; is not in the ring, where the list is.  Keeps all registers.
find_rewind:
        movem.l d0-d2/a0,-(sp)
        move.l  V_SCRP,V_RSCRP
        tst.w   V_CHEAT
        beq.s   .x
        lea     V_URING,a0
        move.w  V_RINGI,d1
        moveq   #15,d2
.f:     move.w  0(a0,d1.w),d0
        cmp.w   V_RESUME,d0
        beq.s   .hit
        subq.w  #8,d1
        andi.w  #$78,d1
        dbra    d2,.f
        bra.s   .x
.hit:   move.l  4(a0,d1.w),V_RSCRP
.x:     movem.l (sp)+,d0-d2/a0
        rts

; ---- a pause resumes: in a cheat map, the press list back to V_RSCRP.  Keeps all registers.
rewind:
        tst.w   V_CHEAT
        beq.s   .x
        move.l  V_RSCRP,V_SCRP
.x:     rts

; ---- main thread: switch to the requested unit / file offset.  The handler shows nothing new while V_REQ is set,
; so it never sees the stream state half changed; V_REQ is cleared last.
do_switch:
        move.w  #$0089,d0               ; CDCSTOP
        jsr     $5F22.w
        move.w  sr,-(sp)
        ori.w   #$0700,sr               ; (the handler writes this register too)
        bset    #0,V_CHOFF+1            ; the audio channel of the clip off (a beep plays on)
        move.b  V_CHOFF+1,$FF0011
        move.w  (sp)+,sr
        moveq   #0,d0
        move.w  V_REQ,d0
        subq.w  #1,d0
        move.w  d0,V_UNIT
        bsr     ring_push
        move.w  d0,d1                   ; visited (COND rules)
        lsr.w   #3,d1
        lea     V_VISIT,a0
        bset    d0,0(a0,d1.w)
        lsl.w   #4,d0
        lea     LOGIC+8,a0
        adda.w  d0,a0
        move.l  a0,V_UPTR
        move.l  U_LBA(a0),V_LBA
        move.w  U_NSECT(a0),V_NSECT
        move.l  V_REQOFF,d0
        move.l  d0,V_VP
        lsr.l   #8,d0
        lsr.l   #3,d0                   ; sector of that offset
        move.w  d0,V_SEC
        lsl.l   #8,d0
        lsl.l   #3,d0
        move.l  d0,V_WR                 ; the ring holds whole sectors
        clr.w   V_RUN
        clr.w   V_EOF
        clr.w   V_PRE
        clr.w   V_STARTED
        clr.w   V_WAIT
        clr.w   V_FCH
        clr.w   V_CHUNK
        move.w  #1,V_FRESH
        move.w  V_REQFR,d1
        move.w  d1,V_FRAME
        moveq   #0,d2                   ; first decision still to come (its window ends after this frame)
        move.w  U_NDEC(a0),d3
        lea     LOGIC,a1
        adda.w  U_DECOFF(a0),a1
        bra.s   .dc
.dl:    move.w  D_FRAME(a1),d4
        add.w   D_WINDOW(a1),d4
        cmp.w   d1,d4
        bhi.s   .dx
        addq.w  #1,d2
        lea     20(a1),a1
.dc:    cmp.w   d3,d2
        blo.s   .dl
.dx:    move.w  d2,V_DECI
        clr.w   V_PRESS
        clr.w   V_REQ
        rts

; ---- streaming reader, one step: take the next sector from the drive into the ring if one has arrived.
; One ROMREADN covers the rest of the file, so the drive reads continuously at full speed (the loader ReadCD
; stops and seeks again on every call).  The drive is stopped only when the ring is full, and restarted (one seek)
; once 64 KB are free again.  At the end of the file it stops.
pump:
        tst.w   V_EOF
        beq.s   .go
        tst.w   V_PRE
        bne     .x                      ; already reading the next unit
        bsr     predict
        tst.w   d0
        bmi     .x                      ; not known yet
        move.w  d0,d1                   ; read it ahead: its file follows this one in the ring
        addq.w  #1,d1
        move.l  V_WR,V_NBASE
        lsl.w   #4,d0
        lea     LOGIC+8,a0
        adda.w  d0,a0
        move.l  U_LBA(a0),V_LBA
        move.w  U_NSECT(a0),V_NSECT
        clr.w   V_SEC
        clr.w   V_RUN
        clr.w   V_EOF
        move.w  d1,V_PRE                ; last: the handler may switch to it from now on
        rts
.go:    move.l  V_WR,d2
        sub.l   V_VP,d2                 ; bytes buffered and not yet taken by the video side
        tst.w   V_RUN
        bne.s   .run
        cmpi.l  #RINGSIZE-CHUNKBYTES,d2
        bgt.s   .x                      ; stopped: restart once 64 KB are free
        move.w  #$0089,d0               ; CDCSTOP
        jsr     $5F22.w
        moveq   #0,d0
        move.w  V_SEC,d0
        moveq   #0,d1
        move.w  V_NSECT,d1
        sub.l   d0,d1                   ; sectors left in the file
        add.l   V_LBA,d0
        lea     RPARAM,a0
        move.l  d0,(a0)
        move.l  d1,4(a0)
        move.w  #$0020,d0               ; ROMREADN: a0 -> start sector, sector count
        jsr     $5F22.w
        move.w  #1,V_RUN
.x:     rts
.run:   cmpi.l  #RINGSIZE-4096,d2
        ble.s   .room
        move.w  #$0089,d0               ; ring full: CDCSTOP, read on later
        jsr     $5F22.w
        clr.w   V_RUN
        rts
.room:  move.w  #$008A,d0               ; CDCSTAT
        jsr     $5F22.w
        bcs.s   .x                      ; no sector ready yet
        andi.w  #$F8FF,$FFFF8004.w      ; CDC destination: sub CPU read
        ori.w   #$0300,$FFFF8004.w
.rd:    move.w  #$008B,d0               ; CDCREAD
        jsr     $5F22.w
        bcs.s   .rd
        move.l  V_WR,d0
        andi.l  #RINGMASK,d0
        lea     RING,a0
        adda.l  d0,a0                   ; destination in the ring
        lea     RHDR,a1
        move.w  #$008C,d0               ; CDCTRN: 2048 bytes to (a0), header to (a1)
        jsr     $5F22.w
        bcs.s   .fail
        move.w  #$008D,d0               ; CDCACK
        jsr     $5F22.w
        bsr     hdr_lba                 ; after a switch the controller can still hold sectors of the previous
        moveq   #0,d1                   ; read: keep only the sector we asked for
        move.w  V_SEC,d1
        add.l   V_LBA,d1
        cmp.l   d1,d0
        beq.s   .ok
        bhi.s   .fail                   ; ahead of it: read again from the sector we need
        rts                             ; an older sector: drop it
.ok:    addi.l  #2048,V_WR
        move.w  V_SEC,d0
        addq.w  #1,d0
        move.w  d0,V_SEC
        cmp.w   V_NSECT,d0
        blo.s   .x2
        move.w  #1,V_EOF                ; the whole file is in
        clr.w   V_RUN
.x2:    rts
.fail:  clr.w   V_RUN                   ; transfer failed: restart from this sector
        rts

; ---- d0.w = the unit that will follow this one, when it is known already (else -1): the route chosen, the clip
; to return to after a revive clip, or (all decisions past) the next unit
predict:
        moveq   #-1,d0
        tst.w   V_REQ
        bne.s   .x
        tst.w   V_PAUSED
        bne.s   .x
        movea.l V_UPTR,a0
        btst    #1,U_FLAGS+1(a0)
        bne.s   .x                      ; the save menu
        move.w  V_ROUTE,d1
        cmpi.w  #$FFFF,d1
        bne.s   .k
        btst    #0,U_FLAGS+1(a0)
        beq.s   .nr
        move.w  V_RETURN,d1             ; a revive clip
        cmpi.w  #$FFFF,d1
        bne.s   .k
        move.w  LOGIC+2,d1
        bra.s   .k
.nr:    move.w  V_DECI,d1
        cmp.w   U_NDEC(a0),d1
        blo.s   .x                      ; decisions to come
        move.w  U_NEXT(a0),d1
        cmpi.w  #$FFFF,d1
        beq.s   .x
.k:     move.w  d1,d0
.x:     rts

; d0.l = sector number in the header CDCTRN left at RHDR (minute, second, frame in BCD, mode): (m*60+s)*75+f-150
hdr_lba:
        lea     RHDR,a0
        bsr.s   .bcd
        mulu.w  #60,d0
        move.l  d0,d1
        bsr.s   .bcd
        add.l   d0,d1
        mulu.w  #75,d1
        bsr.s   .bcd
        add.l   d1,d0
        subi.l  #150,d0
        rts
.bcd:   moveq   #0,d0                   ; next BCD byte (a0)+ -> d0.l
        move.b  (a0)+,d0
        move.w  d0,d2
        lsr.w   #4,d2
        mulu.w  #10,d2
        andi.w  #$0F,d0
        add.w   d2,d0
        rts

; ---- copy d1 bytes (even) from stream offset d0 to (a1): a1 is advanced; clobbers d0-d3,a0
copy_ring:
        andi.l  #RINGMASK,d0
        lea     RING,a0
        adda.l  d0,a0
        move.l  #RINGSIZE,d3
        sub.l   d0,d3                   ; bytes until the end of the ring
        cmp.l   d3,d1
        bls.s   .one
        sub.l   d3,d1
        move.l  d3,d0
        bsr.s   cpy
        lea     RING,a0                 ; wrap
.one:   move.l  d1,d0
        bsr.s   cpy
        rts

; copy d0 bytes (even) from (a0) to (a1); both advance; clobbers d0,d2
cpy:    move.l  d0,d2
        lsr.l   #2,d0
        beq.s   .tail
.l:     move.l  (a0)+,(a1)+
        subq.l  #1,d0
        bne.s   .l
.tail:  btst    #1,d2
        beq.s   .x
        move.w  (a0)+,(a1)+
.x:     rts

; copy d0 bytes (multiple of 4) from (a0) to (a1); both advance; clobbers d0
cpyl:   lsr.w   #2,d0
        subq.w  #1,d0
.l:     move.l  (a0)+,(a1)+
        dbra    d0,.l
        rts

; ---- 1200 audio bytes at stream offset d0 -> PCM chunk V_CHUNK, then advance V_CHUNK; clobbers d0-d5,a0,a1
audio_from:
        lea     AUDBUF,a1
        move.l  #1200,d1
        bsr     copy_ring
        move.w  V_CHUNK,d0
        move.w  d0,-(sp)
        bsr     pcm_write
        move.w  (sp)+,d0
        addq.w  #1,d0
        cmpi.w  #12,d0
        blo.s   .keep
        moveq   #0,d0
.keep:  move.w  d0,V_CHUNK
        rts

; ---- PCM chip (RF5C164): registers at $FF0001+2n, wave RAM window at $FF2001+2n (4 KB, bank selected via control reg)
pcm_init:
        move.b  #$FF,$FF0011            ; all channels off
        move.b  #$C0,$FF000F            ; chip on, MOD=1: select channel 0
        move.b  #$FF,$FF0001            ; ENV
        move.b  #$FF,$FF0003            ; PAN: full left and right
        move.b  #$8A,$FF0005            ; FDL  \ FD = $038A  -> 14,400 Hz (measured 1000.04 Hz from a 1 kHz tone)
        move.b  #$03,$FF0007            ; FDH  /
        move.b  #$00,$FF0009            ; LSL  \ loop back to sample 0
        move.b  #$00,$FF000B            ; LSH  /
        move.b  #$00,$FF000D            ; ST   start at sample 0
        move.b  #$C1,$FF000F            ; channel 1: the beeps
        move.b  #$FF,$FF0001            ; ENV
        move.b  #$FF,$FF0003            ; PAN
        move.b  #$00,$FF0005            ; FD = $0800: one sample per output tick (32,552 Hz, see beeps.py)
        move.b  #$08,$FF0007
        move.b  #BEEP_BASE&$FF,$FF0009  ; a finished beep loops on the silent pair at BEEP_BASE
        move.b  #BEEP_BASE>>8,$FF000B
        move.b  #BEEP_BASE>>8,$FF000D
        move.b  #$83,$FF000F            ; wave bank 3
        move.b  #$FF,$FF3081            ; loop marker right after sample 14399 (bank 3, offset 2112)
        move.w  #$FF,V_CHOFF
        rts

; ---- d0.b = start register of a beep (BEEP_GOOD / BEEP_BAD): play it on channel 1 (restarts it if playing)
beep:
        move.b  #$C1,$FF000F            ; select channel 1
        move.b  d0,$FF000D              ; ST
        move.w  V_CHOFF,d0
        ori.b   #2,d0
        move.b  d0,$FF0011              ; off ...
        moveq   #40,d0
.w:     dbra    d0,.w                   ; (over one output sample: the chip latches ST while the channel is off)
        bclr    #1,V_CHOFF+1
        move.b  V_CHOFF+1,$FF0011       ; ... and on from ST
        rts

; ---- d0.w = chunk 0..11; writes AUDBUF (1200 bytes) to PCM sample address chunk*1200
pcm_write:
        mulu.w  #1200,d0
        lea     AUDBUF,a0
        move.w  #1200,d1
; ---- a0 = source, d0.w = wave RAM address, d1.w = bytes (> 0)
pcm_copy:
.page:  move.w  d0,d2
        lsr.w   #8,d2
        lsr.w   #4,d2                   ; bank = address >> 12
        ori.b   #$80,d2                 ; chip on, MOD=0: select wave bank
        move.b  d2,$FF000F
        move.w  d0,d3
        andi.w  #$0FFF,d3               ; offset inside the bank
        move.w  #4096,d4
        sub.w   d3,d4                   ; room left in this bank
        cmp.w   d4,d1
        bhs.s   .n1
        move.w  d1,d4
.n1:    lea     $FF2001,a1
        move.w  d3,d5
        add.w   d5,d5
        adda.w  d5,a1                   ; wave RAM sits on odd addresses only
        move.w  d4,d5
        subq.w  #1,d5
.cp:    move.b  (a0)+,(a1)
        addq.l  #2,a1
        dbra    d5,.cp
        add.w   d4,d0
        sub.w   d4,d1
        bne.s   .page
        rts

; d0.w = PCM ring chunk (0..11) channel 0 is playing now: its sample address is readable at $FF0023 (high) and
; $FF0021 (low) on the RF5C164
pcm_chunk:
.again: move.b  $FF0023,d1
        move.b  $FF0021,d0
        cmp.b   $FF0023,d1
        bne.s   .again                  ; the high byte moved on while we read: read again
        lsl.w   #8,d1
        move.b  d0,d1
        moveq   #0,d0
        move.w  d1,d0
        divu.w  #1200,d0
        cmpi.w  #12,d0
        blo.s   .ok
        moveq   #11,d0
.ok:    rts

; ---- is the next frame (with any scene record before it) completely in the ring, or the end of the unit reached?
; d0.w = 1 yes / 0 no
peek:
        move.l  V_VP,d5
.l:     move.l  V_WR,d2
        sub.l   d5,d2
        cmpi.l  #6,d2
        blt.s   .no
        move.l  d5,d0
        lea     HDRBUF,a1
        moveq   #6,d1
        bsr     copy_ring
        move.w  HDRBUF,d0
        beq.s   .yes                    ; end of the unit
        move.l  HDRBUF+2,d1
        addq.l  #6,d1
        add.l   d1,d5
        cmpi.w  #2,d0
        bne.s   .l                      ; scene record: look further
        cmp.l   V_WR,d5
        bhi.s   .no
.yes:   moveq   #1,d0
        rts
.no:    moveq   #0,d0
        rts

; ---- static scene record body at stream offset d7: palette, scene tiles, base maps (window = plane rows 2-25)
do_scene:
        clr.w   V_WIDE
        move.l  d7,d0
        lea     WR+WR_PAL,a1
        move.l  #128,d1
        bsr     copy_ring
        move.l  d7,d0
        addi.l  #128,d0
        lea     HDRBUF,a1
        moveq   #4,d1
        bsr     copy_ring
        move.w  HDRBUF,WR+WR_KS
        move.w  HDRBUF+2,d0             ; frame pool size
        bsr     set_pools
        clr.w   WR+WR_VS                ; no scrolling
        clr.w   WR+WR_MASK              ; no border sprites
        lea     BASEA,a1                ; clear both base maps (BASEA and BASEB are contiguous, 4 KB)
        move.w  #1023,d0
.c:     clr.l   (a1)+
        dbra    d0,.c
        move.l  d7,d0
        addi.l  #132,d0
        lea     TMPMAP,a1
        move.l  #1344,d1
        bsr     copy_ring
        lea     BASEB+2*64,a1
        moveq   #23,d1
        bsr     expand
        move.l  d7,d0
        addi.l  #132+1344,d0
        lea     TMPMAP,a1
        move.l  #1344,d1
        bsr     copy_ring
        lea     BASEA+2*64,a1
        moveq   #23,d1
        bsr     expand
        move.l  d7,d0
        addi.l  #132+2688,d0
        bra     scene_tiles

; ---- menu screen record body (type 6, H40) at stream offset d7: palette, tiles, 40 x 28 maps -> the 64-wide
; maps at WR_WMAPB / WR_WMAPA (the main CPU switches to H40, see payload.s status 4)
do_wscene:
        move.l  d7,d0
        lea     WR+WR_PAL,a1
        move.l  #128,d1
        bsr     copy_ring
        move.l  d7,d0
        addi.l  #128,d0
        lea     HDRBUF,a1
        moveq   #4,d1
        bsr     copy_ring
        move.w  HDRBUF,WR+WR_KS
        lea     WR+WR_WMAPB,a4
        move.l  d7,d0
        addi.l  #132,d0
        bsr.s   .map
        lea     WR+WR_WMAPA,a4
        move.l  d7,d0
        addi.l  #132+2240,d0
        bsr.s   .map
        move.l  d7,d0
        addi.l  #132+4480,d0
        move.w  #1,V_WIDE
        bra     scene_tiles
.map:   lea     TMPMAP,a1               ; 28 rows of 40 words -> rows of 64 (the rest 0), 32 rows
        move.l  #2240,d1
        bsr     copy_ring
        lea     TMPMAP,a0
        moveq   #27,d1
.r:     moveq   #39,d0
.w:     move.w  (a0)+,(a4)+
        dbra    d0,.w
        moveq   #11,d0
.z:     clr.l   (a4)+
        dbra    d0,.z
        dbra    d1,.r
        move.w  #4*64/2-1,d0
.zr:    clr.l   (a4)+
        dbra    d0,.zr
        rts

; ---- scrolling scene record body at stream offset d7: palette, border colour, scroll, all 32 plane rows, tiles
do_sscene:
        clr.w   V_WIDE
        move.l  d7,d0
        lea     WR+WR_PAL,a1
        move.l  #128,d1
        bsr     copy_ring
        move.l  d7,d0
        addi.l  #128,d0
        lea     HDRBUF,a1
        moveq   #8,d1
        bsr     copy_ring
        move.w  HDRBUF,WR+WR_KS
        move.w  HDRBUF+2,WR+WR_MASK
        move.w  HDRBUF+4,WR+WR_VS
        move.w  HDRBUF+6,d0             ; frame pool size
        bsr     set_pools
        move.l  d7,d0
        addi.l  #136,d0
        lea     TMPMAP,a1
        move.l  #1792,d1
        bsr     copy_ring
        lea     BASEB,a1
        moveq   #31,d1
        bsr     expand
        move.l  d7,d0
        addi.l  #136+1792,d0
        lea     TMPMAP,a1
        move.l  #1792,d1
        bsr     copy_ring
        lea     BASEA,a1
        moveq   #31,d1
        bsr     expand
        move.l  d7,d0
        addi.l  #136+3584,d0
scene_tiles:                            ; d0 = stream offset of the scene tiles
        lea     WR+WR_ST,a1
        moveq   #0,d1
        move.w  WR+WR_KS,d1
        lsl.l   #5,d1                   ; 32 bytes per tile
        bsr     copy_ring
        rts

; d0.w = frame pool size P: the two pools are the top 2P tiles of VRAM tiles 1-1535 (1536-2P and 1536-P); the
; scene tiles below them are 1..1535-2P (WR_R1, for the main CPU), then 1600-1791 and 1856-2015
set_pools:
        move.w  #1536,d1
        sub.w   d0,d1
        move.w  d1,V_POOL1
        sub.w   d0,d1
        move.w  d1,V_POOL0
        subq.w  #1,d1
        move.w  d1,WR+WR_R1
        rts

; TMPMAP (rows of 28 words) -> (a1) as rows of 32 words: 2 empty words, 28 map words, 2 empty words; d1 = rows-1
expand:
        lea     TMPMAP,a0
.row:   clr.l   (a1)+
        moveq   #13,d0
.w:     move.l  (a0)+,(a1)+
        dbra    d0,.w
        clr.l   (a1)+
        dbra    d1,.row
        rts

; ---- frame record body at stream offset d7: new plane rows, tile slots, then both maps = base maps + this
; frame cells.  Keeps d7.
do_frame:
        move.l  d7,d0
        addi.l  #1200,d0
        lea     HDRBUF,a1
        moveq   #8,d1
        bsr     copy_ring
        move.w  HDRBUF,V_NC
        move.w  HDRBUF+2,V_NT
        move.w  HDRBUF+4,WR+WR_VS       ; vertical scroll of both planes
        move.w  HDRBUF+6,V_NR
        move.l  d7,d0
        addi.l  #1208,d0
        move.l  d0,V_O

; rows coming into view: plane row, tiles, 28 B words, 28 A words -> the base maps; tile runs for the main CPU
        moveq   #0,d4
        move.w  V_NR,d4
        move.w  d4,WR+WR_NRUN
        beq.s   .norows
        move.l  d4,d1
        mulu.w  #116,d1
        move.l  V_O,d0
        add.l   d1,V_O
        lea     ROWBUF,a1
        bsr     copy_ring
        lea     ROWBUF,a0
        lea     WR+WR_RUNS,a3
        subq.w  #1,d4
.row:   move.w  (a0)+,d0                ; plane row
        move.w  (a0)+,d2                ; its tiles
        move.w  d0,d1
        mulu.w  #ROWK,d1
        addq.w  #1,d1
        move.w  d1,(a3)+                ; run: first scene tile (row * ROWK + 1) ...
        move.w  d2,(a3)+                ; ... and how many
        lsl.w   #6,d0                   ; 64 bytes per map row
        lea     BASEB+4,a1
        adda.w  d0,a1
        moveq   #13,d1
.rb:    move.l  (a0)+,(a1)+
        dbra    d1,.rb
        lea     BASEA+4,a1
        adda.w  d0,a1
        moveq   #13,d1
.ra:    move.l  (a0)+,(a1)+
        dbra    d1,.ra
        dbra    d4,.row
.norows:

; cell entries, then the mode words right after them
        moveq   #0,d4
        move.w  V_NC,d4
        move.l  d4,d6
        addi.l  #15,d6
        lsr.l   #4,d6
        add.l   d6,d6                   ; bytes of mode words (one bit per cell)
        move.l  d4,d1
        add.l   d1,d1
        add.l   d6,d1
        move.l  V_O,d0
        add.l   d1,V_O
        lea     ENTBUF,a1
        bsr     copy_ring

; tile references: u16 count, flag words (one bit per tile slot: 1 = reference), reference words
        move.l  V_O,d0
        lea     HDRBUF,a1
        moveq   #2,d1
        bsr     copy_ring
        moveq   #0,d1
        move.w  HDRBUF,d1
        add.l   d1,d1                   ; reference bytes
        moveq   #0,d0
        move.w  V_NT,d0
        addi.l  #15,d0
        lsr.l   #4,d0
        add.l   d0,d0                   ; flag bytes
        move.w  d0,V_FB
        add.l   d0,d1
        move.l  V_O,d0
        addq.l  #2,d0
        move.l  d0,V_O
        add.l   d1,V_O                  ; -> first new tile
        lea     REFBUF,a1
        bsr     copy_ring

; frame pool and tile buffer alternate (the tiles of the previous frame stay readable for references)
        move.w  V_POOL0,d5
        move.l  #WR+WR_FT0,V_FTC
        move.l  #WR+WR_FT1,V_FTP
        bchg    #0,V_PAR+1
        beq.s   .p
        move.w  V_POOL1,d5
        move.l  #WR+WR_FT1,V_FTC
        move.l  #WR+WR_FT0,V_FTP
.p:     move.w  d5,WR+WR_POOL
        move.l  V_FTC,d0
        subi.l  #WR,d0
        move.w  d0,WR+WR_FTO            ; where the main CPU finds the tiles of this frame

; tile slots -> VRAM tiles (SLOTT): new data from the ring and copies of tiles in the pool of the previous frame take the
; next pool tile; a slot equal to an earlier slot of this frame shares the tile of that slot
        moveq   #0,d4
        move.w  V_NT,d4
        lea     REFBUF,a2               ; flag words
        lea     REFBUF,a4
        adda.w  V_FB,a4                 ; reference words
        movea.l V_FTC,a1
        lea     SLOTT,a5
        movea.w WR+WR_POOL,a3           ; next pool tile
        moveq   #0,d5                   ; flag bits left in d6
        subq.w  #1,d4
        bmi.s   .tdone
.t:     subq.w  #1,d5
        bpl.s   .tb
        move.w  (a2)+,d6
        moveq   #15,d5
.tb:    lsr.w   #1,d6
        bcs.s   .ref
        move.l  V_O,d0                  ; new tile
        moveq   #32,d1
        addi.l  #32,V_O
        bsr     copy_ring
        bra.s   .own
.ref:   move.w  (a4)+,d0
        bmi.s   .prev
        andi.w  #$03FF,d0               ; an earlier slot of this frame: share its tile
        add.w   d0,d0
        lea     SLOTT,a0
        move.w  (a0,d0.w),(a5)+
        bra.s   .tn
.prev:  andi.w  #$03FF,d0               ; a tile in the pool of the previous frame: copy it
        lsl.w   #5,d0
        movea.l V_FTP,a0
        adda.w  d0,a0
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
        move.l  (a0)+,(a1)+
.own:   move.w  a3,(a5)+
        addq.w  #1,a3
.tn:    dbra    d4,.t
.tdone: move.w  a3,d0
        sub.w   WR+WR_POOL,d0
        move.w  d0,WR+WR_KF             ; pool tiles to upload

; the tiles of the new rows follow the new tiles of the frame
        moveq   #0,d1
        move.w  V_NR,d4
        lea     WR+WR_RUNS+2,a0
        bra.s   .sc
.s:     add.w   (a0),d1
        addq.l  #4,a0
.sc:    dbra    d4,.s
        lsl.l   #5,d1
        beq.s   .nort
        move.l  V_O,d0
        lea     WR+WR_RT,a1
        bsr     copy_ring
.nort:

; maps: the base maps, then the cells of this frame patched in
        lea     BASEA,a0
        lea     WR+WR_MAPA,a1
        move.w  #2048,d0
        bsr     cpyl
        lea     BASEB,a0
        lea     WR+WR_MAPB,a1
        move.w  #2048,d0
        bsr     cpyl

        lea     SLOTT,a5                ; the tile of each slot, in entry order
        moveq   #0,d4
        move.w  V_NC,d4
        lea     ENTBUF,a0
        move.l  d4,d0
        add.l   d0,d0
        lea     (a0,d0.l),a2            ; mode words
        moveq   #0,d3                   ; mode bits left in d6
        lea     WR+WR_MAPA,a3
        lea     WR+WR_MAPB,a4
        subq.w  #1,d4
        bmi.s   .x
.e:     subq.w  #1,d3
        bpl.s   .hb
        move.w  (a2)+,d6                ; next 16 mode bits, entry i = bit i
        moveq   #15,d3
.hb:    move.w  (a0)+,d1
        move.w  d1,d0
        andi.w  #$03FF,d0
        add.w   d0,d0                   ; byte offset in the map images
        btst    #15,d1
        beq.s   .nb
        move.w  d1,d2
        andi.w  #$6000,d2               ; B line
        or.w    (a5)+,d2
        btst    #0,d6
        beq.s   .sh
        ori.w   #$8000,d2               ; priority: this cell is shown normally, not shadowed
.sh:    move.w  d2,(a4,d0.w)
.nb:    btst    #12,d1
        beq.s   .na
        move.w  d1,d2
        andi.w  #$0C00,d2
        lsl.w   #3,d2                   ; A line -> bits 13-14
        or.w    (a5)+,d2
        move.w  (a4,d0.w),d1            ; the plane B word of the cell (new or the scene)
        andi.w  #$8000,d1
        or.w    d1,d2                   ; same priority as plane B: with Shadow/Highlight on, priority also
        move.w  d2,(a3,d0.w)            ; orders the planes, and a lower-priority A would vanish under B
        bra.s   .nx
.na:    clr.w   (a3,d0.w)               ; nothing on plane A in this cell
.nx:    lsr.w   #1,d6
        dbra    d4,.e
.x:     rts

; ---- interrupt handler: the main CPU asks for the next frame once per vertical blank
handler:
        movem.l d0-d7/a0-a6,-(sp)
        move.w  #30000,d0
.dm:    btst    #1,$FFFF8003.w          ; wait until the sub CPU owns Word RAM
        dbne    d0,.dm
        clr.w   WR+WR_STATUS
        move.l  V_WR,d0                 ; debug: backdrop colour = buffered bytes / 4 KB (red = low 3 bits,
        sub.l   V_VP,d0                 ; green = high 3 bits); blue is added below when a due frame is not
        bpl.s   .pos                    ; fully buffered (a stall)
        moveq   #0,d0
.pos:   lsr.l   #8,d0
        lsr.l   #4,d0
        cmpi.l  #63,d0
        bls.s   .lv
        moveq   #63,d0
.lv:    move.w  d0,d1
        andi.w  #7,d1
        add.w   d1,d1                   ; red bits 1-3
        lsl.w   #2,d0
        andi.w  #$E0,d0                 ; green bits 5-7
        or.w    d1,d0
        move.w  d0,WR+WR_DBG
        ifd     DBGUNIT
        move.w  V_UNIT,d0               ; debug: border = unit (red = low 3 bits, green = next 3), blue 7 while a
        move.w  d0,d1                   ; switch is pending, blue 4 while paused
        andi.w  #7,d1
        add.w   d1,d1
        lsl.w   #2,d0
        andi.w  #$E0,d0
        or.w    d1,d0
        tst.w   V_REQ
        beq.s   .q
        ori.w   #$E00,d0
.q:     tst.w   V_PAUSED
        beq.s   .q2
        ori.w   #$800,d0
.q2:    move.w  d0,WR+WR_DBG
        endif

        addq.w  #1,V_VBL
        move.w  $FFFF8010.w,d0          ; controller (main CPU comm word 0): keep the new presses
        move.w  V_PADP,d1
        move.w  d0,V_PADP
        not.w   d1
        and.w   d1,d0
        or.w    d0,V_PRESS
        bsr     titlectl                ; the start screen; any button skips the intro and the trailers
        tst.w   d0
        bne     .done
        bsr     pausectl                ; START, the pause overlay and the save menu
        tst.w   d0
        bne     .done                   ; paused: nothing new
        tst.w   V_REQ
        bne     .done                   ; a switch is under way
        tst.w   V_STARTED
        beq.s   .ready
        bsr     pcm_chunk               ; video follows the audio clock: is the audio of the next frame playing yet?
        sub.w   V_FCH,d0
        bpl.s   .d
        addi.w  #12,d0
.d:     cmpi.w  #AUD_LEAD,d0
        blo.s   .ready                  ; due (or late)
        addq.w  #1,V_WAIT
        cmpi.w  #20,V_WAIT
        blo     .done                   ; early: answer "nothing new" (after 20 tries go anyway)
.ready: bsr     peek
        tst.w   d0
        bne.s   .go
        ori.w   #$0E00,WR+WR_DBG        ; due but not buffered: stall
        bra     .done
.go:    clr.w   V_WAIT
.next:  move.l  V_VP,d0
        lea     HDRBUF,a1
        moveq   #6,d1
        bsr     copy_ring
        move.w  HDRBUF,d6
        bne.s   .rec
        bsr     unit_end                ; end of the unit: start the next one
        bra     .done
.rec:   move.l  V_VP,d7
        move.l  d7,d5                   ; the offset of the record itself
        addq.l  #6,d7                   ; body
        move.l  HDRBUF+2,d0
        addq.l  #6,d0
        add.l   d0,V_VP
        cmpi.w  #4,d6
        beq.s   .next                   ; old stream preroll record: not used
        cmpi.w  #2,d6
        beq.s   .frame
        cmpi.w  #5,d6
        beq.s   .ss
        cmpi.w  #6,d6
        beq.s   .ws
        bsr     do_scene
        bra.s   .sc
.ws:    bsr     do_wscene               ; a menu screen (H40): status 4
        move.w  #4,WR+WR_STATUS
        bra.s   .sc2
.ss:    bsr     do_sscene
.sc:    move.w  #2,WR+WR_STATUS
.sc2:   tst.w   V_FRESH
        beq.s   .next                   ; a scene record travels with the frame that follows it
        clr.w   V_FRESH                 ; playback starts here: its preroll fills PCM chunks 0..5
        clr.w   V_CHUNK
        move.l  V_VP,d7
        subi.l  #PREROLL,d7
        moveq   #AUD_LEAD-1,d6
.pre:   move.l  d7,d0
        bsr     audio_from
        addi.l  #1200,d7
        dbra    d6,.pre
        bra     .next
.frame: tst.w   V_WIDE
        bne.s   .fs                     ; the frames of a menu screen change nothing on screen
        bsr     do_frame
        tst.w   WR+WR_STATUS
        bne.s   .fs
        move.w  #1,WR+WR_STATUS
.fs:    move.l  d7,d0
        bsr     audio_from              ; this record carries the audio of frame +AUD_LEAD
        move.w  V_FCH,d0
        addq.w  #1,d0
        cmpi.w  #12,d0
        blo.s   .f
        moveq   #0,d0
.f:     move.w  d0,V_FCH
        tst.w   V_STARTED
        bne.s   .dec
        move.w  #1,V_STARTED
        bclr    #0,V_CHOFF+1            ; channel 0 on: audio starts with the first frame
        move.b  V_CHOFF+1,$FF0011
.dec:   bsr     decide
        addq.w  #1,V_FRAME
.done:  tst.w   V_FLASHT
        beq.s   .fl
        subq.w  #1,V_FLASHT
        bne.s   .fl
        clr.w   V_FLASH
.fl:
        ifd     DBGINPUT
        move.w  V_FLASH,WR+WR_DBG       ; debug: backdrop green / red for one frame after a press (payload DEBUG=1)
        endif
        bset    #0,$FFFF8003.w          ; RET: Word RAM back to the main CPU
        movem.l (sp)+,d0-d7/a0-a6
        rts

; ---- after showing frame V_FRAME: test input, then the next decision of this unit
decide:
        clr.w   V_FLASH                 ; a press flash lasts until the next frame (or FLASH_TIME, if none shows)
        ifd     DBGPAUSE                ; hardware test: in a cheat map, START at frame DBGPF of unit DBGPU (once)
        tst.w   V_CHEAT
        beq.s   .ndp
        tst.w   V_DBGP
        bne.s   .ndp
        cmpi.w  #DBGPU,V_UNIT
        bne.s   .ndp
        cmpi.w  #DBGPF,V_FRAME
        bne.s   .ndp
        move.w  #1,V_DBGP
        bset    #B_START,V_PRESS+1
.ndp:
        endif
        movea.l V_SCRP,a0               ; test script: presses tied to a unit frame
        move.w  (a0),d0
        cmp.w   V_UNIT,d0
        bne.s   .ns
        move.w  2(a0),d0
        cmp.w   V_FRAME,d0
        bne.s   .ns
        move.w  4(a0),d0
        or.w    d0,V_PRESS
        addq.l  #8,V_SCRP
.ns:    cmpi.w  #$FFFF,V_ROUTE
        beq.s   .nr
        move.w  V_FRAME,d0
        addq.w  #1,d0
        cmp.w   V_CUT,d0
        blo.s   .nr
        move.w  V_ROUTE,d0              ; the route switch frame is next: go
        bra     take_route
.nr:    movea.l V_UPTR,a1
        move.w  V_DECI,d1
        cmp.w   U_NDEC(a1),d1
        bhs     .clr                    ; no decisions left
        mulu.w  #20,d1
        lea     LOGIC,a2
        adda.w  U_DECOFF(a1),a2
        adda.w  d1,a2                   ; this decision
        move.w  V_FRAME,d2
        cmp.w   D_FRAME(a2),d2
        blo     .clr                    ; its window is not open yet
        move.w  D_FRAME(a2),d3
        add.w   D_WINDOW(a2),d3
        cmp.w   d3,d2
        bhs.s   .late                   ; window over without a press: outcome 0
        move.w  V_PRESS,d0
        andi.w  #$1F,d0                 ; up, down, left, right, fire = outcomes 1-5
        beq     .x
        moveq   #0,d1
.bit:   addq.w  #1,d1
        lsr.w   #1,d0                   ; the first button pressed
        bcc.s   .bit
        andi.w  #$FFE0,V_PRESS
        bra.s   .apply
.late:  moveq   #0,d1
.apply: add.w   d1,d1
        move.w  D_OUT(a2,d1.w),d0       ; outcome
        tst.w   D_KIND(a2)
        bne.s   .route
        addq.w  #1,V_DECI               ; death decision: $FFFF = the right move
        cmpi.w  #$FFFF,d0
        bne.s   .wrong
        tst.w   d1
        bne     good                    ; (the right move can also be no input: no sound then)
        rts
.wrong: move.w  V_UNIT,V_RETURN         ; wrong: the death scene, then the revive clip, then this clip again
        move.w  d1,-(sp)                ; (request uses d1)
        bsr     request
        tst.w   (sp)+
        bne     bad                     ; a wrong move: the bad beep; the window ran out: no sound (as the 3DO)
        rts
.route: tst.w   d1                      ; every move in the window chooses again, the last one wins (as the 3DO)
        bne.s   .rp
        cmpi.w  #$FFFF,V_ROUTE
        beq.s   .rp
        addq.w  #1,V_DECI               ; window over: the way chosen in it stands
        rts
.rp:    bsr     route_out               ; lists (one at random) and COND rules
        cmpi.w  #$FFFF,d0
        bne.s   .take
        tst.w   d1
        beq.s   .rn
        move.w  #$FFFF,V_ROUTE          ; not offered: the bad beep, and a way chosen before is dropped
        bra     bad
.rn:    addq.w  #1,V_DECI               ; window over, nothing chosen: the clip plays on (a death for waiting)
        rts
.take:  move.w  d0,V_ROUTE              ; route chosen: taken at the decision switch frame
        move.w  D_CUT(a2),V_CUT
        tst.w   d1
        bne     good                    ; (the window stays open: a later move can change it)
        addq.w  #1,V_DECI               ; a way taken with no move, at the window's end: no sound
        rts
.clr:   move.b  U_FLAGS+1(a1),d0
        andi.b  #$70,d0
        bne.s   .x                      ; the menus, the intro and the trailers: titlectl takes the press
        move.w  V_PRESS,d0
        andi.w  #$FFE0,V_PRESS          ; presses outside a window do nothing but the bad beep (as the 3DO)
        andi.w  #$1F,d0
        bne     bad
.x:     rts

; ---- d0 = a route outcome of unit a1 -> the unit it leads to ($FFFF = not offered): a list ($8000 | word offset
; in LOGIC.BIN: count, units) gives one of its units at random; then the COND rules of the unit (after its decisions:
; count, then per rule target, replacement, count, units): once all those units have been visited, the target
; becomes the replacement.  Keeps d1, a1, a2.
route_out:
        movem.l d1-d7/a3-a4,-(sp)
        cmpi.w  #$FFFF,d0
        beq.s   .x
        btst    #15,d0
        beq.s   .c
        andi.l  #$7FFF,d0               ; a list
        add.l   d0,d0
        lea     LOGIC,a3
        adda.l  d0,a3
        bsr     random
        mulu.w  (a3)+,d0
        swap    d0                      ; 0 .. count-1
        add.w   d0,d0
        move.w  0(a3,d0.w),d0
.c:     btst    #3,U_FLAGS+1(a1)
        beq.s   .x
        lea     LOGIC,a3                ; the COND rules follow the decisions of the unit
        adda.w  U_DECOFF(a1),a3
        move.w  U_NDEC(a1),d2
        mulu.w  #20,d2
        adda.l  d2,a3
        move.w  (a3)+,d2                ; rules
        bra.s   .rn
.r:     move.w  (a3)+,d3                ; target
        move.w  (a3)+,d4                ; replacement
        move.w  (a3)+,d5                ; units that must have been visited
        moveq   #1,d6                   ; all visited so far
        bra.s   .vn
.v:     move.w  (a3)+,d7                ; unit: bit (unit & 7) of byte V_VISIT + unit / 8
        move.w  d7,d1
        lsr.w   #3,d1
        lea     V_VISIT,a0
        btst    d7,0(a0,d1.w)
        bne.s   .vn
        moveq   #0,d6
.vn:    dbra    d5,.v
        cmp.w   d3,d0
        bne.s   .rn
        tst.w   d6
        beq.s   .rn
        move.w  d4,d0                   ; the rule applies
.rn:    dbra    d2,.r
.x:     movem.l (sp)+,d1-d7/a3-a4
        rts

; ---- d0.w = a random number (linear congruential, mixed with the vertical blank count)
random:
        move.w  V_RAND,d0
        mulu.w  #25173,d0
        addi.w  #13849,d0
        add.w   V_VBL,d0
        move.w  d0,V_RAND
        andi.l  #$FFFF,d0
        rts

; ---- go to route d0 (its switch frame): a death clip of its own (unit flag bit 2) returns here after its revive
take_route:
        move.w  d0,d1
        lsl.w   #4,d1
        lea     LOGIC+8,a0
        btst    #2,U_FLAGS+1(a0,d1.w)
        beq     request
        move.w  V_UNIT,V_RETURN
        bra     request

; ---- an accepted / refused press: its beep, and the DBGINPUT backdrop flash
good:   move.w  #FLASH_GOOD,V_FLASH
        move.w  #FLASH_TIME,V_FLASHT
        moveq   #BEEP_GOOD,d0
        bra     beep
bad:    move.w  #FLASH_BAD,V_FLASH
        move.w  #FLASH_TIME,V_FLASHT
        moveq   #BEEP_BAD,d0
        bra     beep

; ---- the start screen (3DO SELECTBG / SELECT.BIN; unit flag bit 5) and the clips any button skips (bit 4: the
; intro and the trailers, which lead to the start screen).  Start screen: up / down / left / right move the
; outline to the nearest button that way (TITLE.BIN), A/B/C or START presses it; every action plays the bad beep
; (as the 3DO does).  New Game: the first clip, nothing visited; Continue: the saved clip and visited clips (a new
; game if there is no save); a trailer or the intro: plays it.  d0 = 1: nothing else this vertical blank.
titlectl:
        movea.l V_UPTR,a0
        btst    #6,U_FLAGS+1(a0)
        bne     cheatctl
        btst    #5,U_FLAGS+1(a0)
        bne.s   .title
        btst    #4,U_FLAGS+1(a0)
        beq.s   .no
        move.w  V_PRESS,d0              ; skippable: any button goes to the start screen
        andi.w  #$9F,d0
        beq.s   .no
        clr.w   V_PRESS
        tst.w   V_REQ
        bne.s   .hold
        move.w  TITLEB,d0
        cmpi.w  #$FFFF,d0
        beq.s   .no
        clr.w   V_TSHOWN
        bsr     request
        bra.s   .hold
.no:    moveq   #0,d0
        rts
.title: tst.w   V_REQ
        bne.s   .hold
        tst.w   V_TSHOWN
        bne.s   .act
        tst.w   V_FRAME
        beq.s   .no                     ; its frame is still to come: play it
        move.w  #1,V_TSHOWN             ; on screen: no sound, the outline of the selected button
        clr.w   V_TSEL
        clr.w   V_CODE
        clr.w   V_PWAIT
        bset    #0,V_CHOFF+1
        move.b  V_CHOFF+1,$FF0011
        clr.w   V_PRESS
        bsr     title_ol
.hold:  moveq   #1,d0
        rts
.act:   bsr     script_wait
        move.w  V_PRESS,d1
        clr.w   V_PRESS
        andi.w  #$9F,d1
        beq.s   .hold
        moveq   #BEEP_BAD,d0            ; every action: the bad beep
        bsr     beep
        bsr     cheatcode
        tst.w   d0
        bne.s   .hold                   ; the code is complete: the cheat menu
        lea     TITLEB+6,a1
        move.w  V_TSEL,d2
        lsl.w   #4,d2
        adda.w  d2,a1                   ; the selected button: action, unit, up, down, left, right, sprites
        moveq   #0,d2
.dir:   btst    d2,d1
        beq.s   .dn
        move.w  d2,d3
        add.w   d3,d3
        move.w  4(a1,d3.w),d3           ; the button that way
        cmpi.w  #$FFFF,d3
        beq.s   .hold
        move.w  d3,V_TSEL
        bsr     title_ol
        bra.s   .hold
.dn:    addq.w  #1,d2
        cmpi.w  #4,d2
        blo.s   .dir
        move.w  (a1),d2                 ; A / B / C / START: press it
        move.w  2(a1),d0
        cmpi.w  #1,d2
        bne.s   .nc
        tst.w   V_SAVEOK                ; Continue
        beq.s   .new
        lea     SAVEBUF+8,a2            ; the visited clips, then the saved clip
        lea     V_VISIT,a3
        moveq   #15,d3
.cp:    move.l  (a2)+,(a3)+
        dbra    d3,.cp
        move.w  SAVEBUF+6,d0
        bra.s   .go
.nc:    cmpi.w  #2,d2
        beq.s   .go                     ; a trailer or the intro
.new:   move.w  TITLEB+2,d0             ; New Game: nothing visited
        lea     V_VISIT,a3
        moveq   #15,d3
.cv:    clr.l   (a3)+
        dbra    d3,.cv
.go:    clr.w   V_TSHOWN
        bsr     request
        bra     .hold

; ---- test script: presses while paused or on the start screen (P:n:button entries, unit $FFFE), n vertical
; blanks after the previous one.  Keeps d0.
script_wait:
        movea.l V_SCRP,a0
        cmpi.w  #$FFFE,(a0)
        bne.s   .x
        addq.w  #1,V_PWAIT
        move.w  V_PWAIT,d1
        cmp.w   2(a0),d1
        blo.s   .x
        move.w  4(a0),d1
        or.w    d1,V_PRESS
        addq.l  #8,V_SCRP
        clr.w   V_PWAIT
.x:     rts

; ---- the 3DO cheat code on the start screen (launchme 0xE334): d1 = the buttons pressed.  Up counts when the count
; is even, Down when it is odd, either resets it otherwise; Right resets it; Left with the count at 8 opens the cheat
; menu (the 3DO test menu).  d0 = 1: it opened.  Keeps d1.
cheatcode:
        move.w  V_CODE,d0
        btst    #0,d1                   ; up
        beq.s   .nu
        btst    #0,d0
        bne.s   .r1
        addq.w  #1,d0
        bra.s   .nu
.r1:    moveq   #0,d0
.nu:    btst    #1,d1                   ; down
        beq.s   .nd
        btst    #0,d0
        beq.s   .r2
        addq.w  #1,d0
        bra.s   .nd
.r2:    moveq   #0,d0
.nd:    btst    #3,d1                   ; right
        beq.s   .nr
        moveq   #0,d0
.nr:    move.w  d0,V_CODE
        btst    #2,d1                   ; left
        beq.s   .no
        cmpi.w  #8,d0
        bne.s   .no
        clr.w   V_CODE
        move.w  CHEATB,d0
        cmpi.w  #$FFFF,d0
        beq.s   .no                     ; (a disc with no cheat menu)
        clr.w   V_TSHOWN
        clr.w   V_CSHOWN
        bsr     request
        moveq   #1,d0
        rts
.no:    moveq   #0,d0
        rts

; ---- the cheat menu (3DO TESTBACK / SELTEST.BIN; unit flag bit 6): Right or Down selects the next entry, Left or Up
; the one before (CHEATS.BIN), A / C / START plays its map: a new game from the first clip of the map, its presses made
; for you (its press list becomes the test script); B goes back to the start screen (the 3DO STOP button).  Every
; action plays the bad beep, as on the start screen.  d0 = 1: nothing else this vertical blank.
cheatctl:
        tst.w   V_REQ
        bne.s   .hold
        tst.w   V_CSHOWN
        bne.s   .act
        tst.w   V_FRAME
        beq.s   .no                     ; its frame is still to come: play it
        move.w  #1,V_CSHOWN             ; on screen: no sound, the box round the selected entry
        clr.w   V_CSEL
        clr.w   V_PWAIT
        bset    #0,V_CHOFF+1
        move.b  V_CHOFF+1,$FF0011
        clr.w   V_PRESS
        lea     CHEATB,a3
        moveq   #0,d0
        bsr     menu_ol
.hold:  moveq   #1,d0
        rts
.no:    moveq   #0,d0
        rts
.act:   bsr     script_wait
        move.w  V_PRESS,d1
        clr.w   V_PRESS
        btst    #9,d1
        beq.s   .nb
        moveq   #BEEP_BAD,d0            ; B: the start screen
        bsr     beep
        move.w  TITLEB,d0
        clr.w   V_CSHOWN
        bsr     request
        bra.s   .hold
.nb:    andi.w  #$9F,d1
        beq.s   .hold
        moveq   #BEEP_BAD,d0
        bsr     beep
        lea     CHEATB+6,a1
        move.w  V_CSEL,d2
        lsl.w   #4,d2
        adda.w  d2,a1                   ; the selected entry: 3, first unit, up, down, left, right, box, press list
        moveq   #0,d2
.dir:   btst    d2,d1
        beq.s   .dn
        move.w  d2,d3
        add.w   d3,d3
        move.w  4(a1,d3.w),d3           ; the entry that way
        cmpi.w  #$FFFF,d3
        beq.s   .hold
        move.w  d3,V_CSEL
        lea     CHEATB,a3
        move.w  d3,d0
        bsr     menu_ol
        bra.s   .hold
.dn:    addq.w  #1,d2
        cmpi.w  #4,d2
        blo.s   .dir
        moveq   #0,d2                   ; A / C / START: play the map
        move.w  14(a1),d2
        lea     CHEATB,a2
        adda.l  d2,a2
        move.l  a2,V_SCRP               ; its presses
        move.w  #1,V_CHEAT
        lea     V_VISIT,a3              ; a new game: nothing visited
        moveq   #15,d3
.cv:    clr.l   (a3)+
        dbra    d3,.cv
        move.w  #$FFFF,V_RETURN
        move.w  2(a1),d0                ; from its first clip
        clr.w   V_CSHOWN
        bsr     request
        bra     .hold

; ---- the selected button outline as sprites (TITLE.BIN: its block of sprites and 4-bit tiles, line 3)
title_ol:
        lea     TITLEB,a3
        move.w  V_TSEL,d0
; ---- a menu selection as sprites: a3 = TITLE.BIN or CHEATS.BIN (6 bytes, then 16 per entry, its block offset at
; +12), d0 = the selected entry
menu_ol:
        lea     6(a3),a2
        lsl.w   #4,d0
        move.w  12(a2,d0.w),d0          ; block offset
        andi.l  #$FFFF,d0
        movea.l a3,a2
        adda.l  d0,a2
        move.w  (a2)+,d7                ; tiles
        move.w  (a2)+,d6                ; sprites
        move.w  #1400,WR+WR_X+X_V       ; the sprite tiles of the menu screens
        lea     WR+WR_X+X_SPR,a1
        moveq   #0,d4
.s:     move.w  (a2)+,(a1)+             ; y
        move.w  d4,d2
        addq.w  #1,d2
        cmp.w   d6,d2
        blo.s   .lk
        moveq   #-16,d2
.lk:    addi.w  #16,d2
        or.w    (a2)+,d2                ; size, link
        move.w  d2,(a1)+
        move.w  (a2)+,d2
        addi.w  #$E000+1400,d2          ; priority, line 3, tile
        move.w  d2,(a1)+
        move.w  (a2)+,(a1)+             ; x
        addq.w  #1,d4
        cmp.w   d6,d4
        blo.s   .s
        lea     WR+WR_X+X_TILES,a1
        move.w  d7,d0
        lsl.w   #3,d0                   ; longs
        subq.w  #1,d0
.t:     move.l  (a2)+,(a1)+
        dbra    d0,.t
        move.w  d7,WR+WR_X+X_N
        move.w  d6,WR+WR_X+X_S
        move.w  #XF_SPR,WR+WR_X+X_F
        move.w  #3,WR+WR_STATUS
        rts

; ---- backup RAM (main thread): at boot set it up (format it if it has never been) and read the save;
; bram_save writes SAVEBUF.  A save of another build of the disc (LOGIC.BIN build id) is ignored.
bram_load:
        clr.w   V_SAVEOK
        clr.w   V_BRAM
        lea     BRAMWORK,a0
        lea     BRAMSTR,a1
        moveq   #0,d0                   ; BRMINIT
        jsr     _BURAM
        bcc.s   .ok
        cmpi.w  #1,d1
        bne.s   .x                      ; no backup RAM, or another format: leave it alone
        moveq   #6,d0                   ; BRMFORMAT (never formatted)
        jsr     _BURAM
        bcs.s   .x
.ok:    move.w  #1,V_BRAM
        lea     name_save(pc),a0
        lea     SAVEBUF,a1
        moveq   #3,d0                   ; BRMREAD
        jsr     _BURAM
        bcs.s   .x
        cmpi.l  #$42443133,SAVEBUF      ; the text BD13
        bne.s   .x
        move.w  LOGIC+4,d0
        cmp.w   SAVEBUF+4,d0
        bne.s   .x
        move.w  #1,V_SAVEOK
.x:     rts

bram_save:
        move.l  #$42443133,SAVEBUF      ; the text BD13
        move.w  LOGIC+4,SAVEBUF+4       ; build id
        move.w  V_RESUME,SAVEBUF+6      ; the clip
        lea     V_VISIT,a0
        lea     SAVEBUF+8,a1
        moveq   #15,d0
.cp:    move.l  (a0)+,(a1)+
        dbra    d0,.cp
        clr.w   V_SAVEREQ
        move.w  #1,V_SAVEOK             ; Continue works from now on, even without backup RAM
        tst.w   V_BRAM
        beq.s   .x
        lea     name_save(pc),a0
        lea     SAVEBUF,a1
        moveq   #4,d0                   ; BRMWRITE
        jsr     _BURAM
.x:     rts

; ---- pause (3DO assets: the PAUSE overlay, the save menu).  Playing: START holds the picture with the PAUSE
; overlay (sprites) and stops the sound.  Overlay: START resumes (the clip file plays again from its start), A/B/C
; opens the save menu (its own unit, a one-frame stream).  Menu: up/down selects, A/B/C or START presses the button:
; Continue resumes, Save does nothing yet (it is to keep the clip and whether the items for the final part are
; collected), Exit restarts the console (there is no title screen yet).  Returns d0 = 0 to play on, 1 to hold.
pausectl:
        move.w  V_PAUSED,d0
        bne.s   .paused
        bclr    #B_START,V_PRESS+1
        beq     .play
        tst.w   V_REQ
        bne     .play                   ; a switch is under way: not now
        move.w  #1,V_PAUSED
        clr.w   V_PWAIT
        move.w  #$FF,V_CHOFF
        move.b  #$FF,$FF0011            ; sound off; the picture stays
        bsr     resume_unit
        move.w  d0,V_RESUME
        bsr     find_rewind
        clr.w   V_BLINK
        move.w  #1,V_BLINKON
        bsr     overlay
        bra     .hold
.paused:
        bsr     script_wait
        ifd     DBGPAUSE                ; hardware test: START again after two seconds paused
        cmpi.w  #1,V_DBGP
        bne.s   .ndq
        addq.w  #1,V_DBGW
        cmpi.w  #120,V_DBGW
        blo.s   .ndq
        move.w  #2,V_DBGP
        bset    #B_START,V_PRESS+1
.ndq:
        endif
.nsc:   cmpi.w  #2,d0
        beq     .menu
        bclr    #B_START,V_PRESS+1      ; the pause overlay
        bne.s   .resume
        btst    #B_ACTION,V_PRESS+1
        beq     .blink
        move.w  LOGIC+6,d0          ; A / B / C: the save menu
        cmpi.w  #$FFFF,d0
        beq.s   .hold
        move.w  #2,V_PAUSED
        clr.w   V_MSHOWN
        lea     menu_tab(pc),a0         ; Continue is selected first
        moveq   #0,d1
.dc:    cmp.w   (a0),d1
        bhs.s   .dcx
        move.w  d1,d2
        lsl.w   #2,d2
        tst.w   2(a0,d2.w)
        beq.s   .dcx
        addq.w  #1,d1
        bra.s   .dc
.dcx:   move.w  d1,V_MSEL
        bsr     request
        bra.s   .hold
.resume:
        clr.w   V_PAUSED
        bsr     rewind
        move.w  V_RESUME,d0
        bsr     request
.hold:  clr.w   V_PRESS
        moveq   #1,d0
        rts
.blink: addq.w  #1,V_BLINK              ; the PAUSE text blinks: on / off every half second
        cmpi.w  #BLINK_TIME,V_BLINK
        blo.s   .hold
        clr.w   V_BLINK
        bchg    #0,V_BLINKON+1
        beq.s   .on                     ; (bit was 0: now on)
        clr.w   WR+WR_X+X_N             ; off: no sprites
        clr.w   WR+WR_X+X_S
        move.w  #XF_SPR,WR+WR_X+X_F
        move.w  #3,WR+WR_STATUS
        bra.s   .hold
.on:    bsr     overlay
        bra.s   .hold
.play:  moveq   #0,d0
        rts
.menu:  tst.w   V_MSHOWN
        bne.s   .mact
        tst.w   V_REQ
        bne.s   .hold
        tst.w   V_FRAME
        beq.s   .play                   ; its frame is still to come: play it
        move.w  #1,V_MSHOWN             ; on screen: no sound, show the selection
        bset    #0,V_CHOFF+1
        move.b  V_CHOFF+1,$FF0011
        bsr     menu_pal
        bra     .hold
.mact:  move.w  V_PRESS,d1
        clr.w   V_PRESS
        lea     menu_tab(pc),a0
        btst    #0,d1                   ; up
        beq.s   .nu
        tst.w   V_MSEL
        beq.s   .nu
        subq.w  #1,V_MSEL
        bsr     menu_pal
.nu:    btst    #1,d1                   ; down
        beq.s   .nd
        move.w  V_MSEL,d2
        addq.w  #1,d2
        cmp.w   (a0),d2
        bhs.s   .nd
        move.w  d2,V_MSEL
        bsr     menu_pal
.nd:    andi.w  #$90,d1                 ; A/B/C or START: press the selected button
        beq.s   .mx
        move.w  V_MSEL,d2
        lsl.w   #2,d2
        move.w  2(a0,d2.w),d2           ; its action
        bne.s   .nc
        clr.w   V_PAUSED                ; Continue
        bsr     rewind
        move.w  V_RESUME,d0
        bsr     request
        bra.s   .mx
.nc:    cmpi.w  #1,d2
        bne.s   .ex
        bsr     good                    ; Save: the clip and the clips visited (the items), to backup RAM
        move.w  #1,V_SAVEREQ
        bra.s   .mx
.ex:    tst.w   V_CHEAT                 ; Exit: a cheat map ends (its press list to its end)
        beq.s   .ex2
        clr.w   V_CHEAT
        movea.l V_SCRP,a1
.ee:    cmpi.w  #$FFFF,(a1)
        beq.s   .ee2
        addq.l  #8,a1
        bra.s   .ee
.ee2:   move.l  a1,V_SCRP
.ex2:   move.w  TITLEB,d0               ; Exit: the start screen
        cmpi.w  #$FFFF,d0
        beq.s   .rb
        clr.w   V_PAUSED
        bsr     request
        bra.s   .mx
.rb:    move.w  #XF_REBOOT,WR+WR_X+X_F  ; (no start screen on this disc: restart the console)
        move.w  #3,WR+WR_STATUS
.mx:    moveq   #1,d0
        rts

; ---- d0 = the clip to play after a pause: this one, or for a death scene / the revive clip the clip it belongs to
resume_unit:
        movea.l V_UPTR,a1
        move.w  V_UNIT,d0
        move.b  U_FLAGS+1(a1),d1
        andi.b  #5,d1                   ; the revive clip or a death clip of its own
        beq.s   .unr
        move.w  V_RETURN,d0             ; the revive clip: the clip where the death happened
        cmpi.w  #$FFFF,d0
        bne.s   .x
        move.w  LOGIC+2,d0
        rts
.unr:   cmpi.w  #$FFFF,U_RESTART(a1)
        beq.s   .x
        move.w  U_RESTART(a1),d0        ; a death scene: its clip
.x:     rts

; ---- the PAUSE overlay: sprites (PAUSE.SPR, two layers: fill and outline) in the lightest and the darkest colour of
; the scene palette (not colour 0, not line 3 colours 14/15, which are shadow/highlight operators for sprites),
; their tiles in the frame pool that is not on screen.  The main CPU writes them after the border sprites.
overlay:
        lea     pause_spr(pc),a2
        move.w  (a2),d7                 ; tiles per layer
        beq     .x
        lea     WR+WR_PAL,a0
        moveq   #-1,d4                  ; brightest so far
        moveq   #99,d3                  ; darkest so far
        moveq   #0,d0
.c:     move.w  d0,d1
        andi.w  #15,d1
        beq.s   .cn
        cmpi.w  #62,d0
        bhs.s   .cn
        move.w  d0,d1
        add.w   d1,d1
        move.w  0(a0,d1.w),d1           ; CRAM word: b << 9 | g << 5 | r << 1
        move.w  d1,d2
        lsr.w   #1,d2
        andi.w  #7,d2
        lsr.w   #5,d1
        move.w  d1,d6
        andi.w  #7,d6
        add.w   d6,d2
        lsr.w   #4,d1
        andi.w  #7,d1
        add.w   d1,d2                   ; r + g + b
        cmp.w   d4,d2
        ble.s   .cd
        move.w  d2,d4
        move.w  d0,d5                   ; the lightest colour
.cd:    cmp.w   d3,d2
        bge.s   .cn
        move.w  d2,d3
        move.w  d0,a4                   ; the darkest
.cn:    addq.w  #1,d0
        cmpi.w  #64,d0
        blo.s   .c
        move.w  WR+WR_POOL,d0           ; tiles: the pool that is not on screen
        move.w  V_POOL0,d6
        cmp.w   d6,d0
        bne.s   .pl
        move.w  V_POOL1,d6
.pl:    move.w  d6,WR+WR_X+X_V
        move.w  2(a2),d3                ; sprites per layer
        lea     4(a2),a3                ; the sprite list
        move.w  d3,d0
        lsl.w   #3,d0
        lea     4(a2,d0.w),a2           ; the fill masks, then the outline masks
        lea     WR+WR_X+X_TILES,a1
        move.w  d5,d0
        bsr.s   .layer                  ; fill: lightest
        move.w  a4,d0
        bsr.s   .layer                  ; outline: darkest
        lea     WR+WR_X+X_SPR,a1        ; sprites: fill first (in front), then outline
        moveq   #0,d4                   ; sprite number
        move.w  d5,d0
        moveq   #0,d1
        bsr.s   .sprites
        move.w  a4,d0
        move.w  d7,d1
        bsr.s   .sprites
        move.w  d7,d0
        add.w   d0,d0
        move.w  d0,WR+WR_X+X_N
        move.w  d4,WR+WR_X+X_S
        move.w  #XF_SPR,WR+WR_X+X_F
        move.w  #3,WR+WR_STATUS
.x:     rts
; d0 = palette index: d7 tiles of 1-bit masks (a2+) -> 4-bit tiles (a1+)
.layer: andi.w  #15,d0
        move.w  d7,d2
        lsl.w   #3,d2
        subq.w  #1,d2                   ; mask rows
.row:   move.b  (a2)+,d1
        moveq   #0,d6
        moveq   #7,d3
.px:    lsl.l   #4,d6
        lsl.b   #1,d1
        bcc.s   .z
        or.b    d0,d6
.z:     dbra    d3,.px
        move.l  d6,(a1)+
        dbra    d2,.row
        rts
; d0 = palette index, d1 = first tile of the layer: one sprite entry per sprite of the list (a3), linked from 16 on
.sprites:
        movea.l a3,a0
        lea     pause_spr(pc),a5
        move.w  2(a5),d3
.s:     move.w  (a0),(a1)+              ; y
        move.w  d4,d2
        addq.w  #1,d2
        move.w  2(a5),d6
        add.w   d6,d6                   ; sprites in all
        cmp.w   d6,d2
        blo.s   .lk
        moveq   #-16,d2                 ; the last links to 0
.lk:    addi.w  #16,d2
        or.w    2(a0),d2                ; size
        move.w  d2,(a1)+
        move.w  d0,d2
        lsr.w   #4,d2                   ; line
        ror.w   #3,d2                   ; -> bits 13-14
        ori.w   #$8000,d2               ; priority: always shown normally
        add.w   WR+WR_X+X_V,d2
        add.w   d1,d2
        add.w   4(a0),d2                ; first tile
        move.w  d2,(a1)+
        move.w  6(a0),(a1)+             ; x
        addq.l  #8,a0
        addq.w  #1,d4
        subq.w  #1,d3
        bne.s   .s
        rts

; ---- the save menu: label colours (menu_tab), the selected one highlighted; the main CPU uploads the palette
menu_pal:
        lea     menu_tab(pc),a2
        move.w  (a2)+,d3                ; buttons
        move.w  d3,d4
        lsl.w   #2,d4
        move.l  0(a2,d4.w),d5           ; label word << 16 | selected label word
        lea     WR+WR_PAL,a3
        moveq   #0,d4
.l:     move.w  2(a2),d2
        add.w   d2,d2
        move.l  d5,d6
        swap    d6                      ; the label colour ...
        cmp.w   V_MSEL,d4
        bne.s   .w
        move.w  d5,d6                   ; ... or the selected one
.w:     move.w  d6,0(a3,d2.w)
        addq.l  #4,a2
        addq.w  #1,d4
        cmp.w   d3,d4
        blo.s   .l
        move.w  #XF_PAL,WR+WR_X+X_F
        move.w  #3,WR+WR_STATUS
        rts

; ---- the unit ended: the death scene goes on to the revive clip, the revive clip back to the clip where the death
; happened, a main path to the route chosen or its JUMP
unit_end:
        move.w  V_ROUTE,d0              ; a route chosen whose switch frame is past the end
        cmpi.w  #$FFFF,d0
        bne     take_route
        movea.l V_UPTR,a1
        btst    #0,U_FLAGS+1(a1)
        beq.s   .nrev
        move.w  V_RETURN,d0             ; the revive clip: back to where the death happened
        cmpi.w  #$FFFF,d0
        bne     request
        move.w  LOGIC+2,d0
        bra     request
.nrev:  move.w  U_RESTART(a1),d0
        cmpi.w  #$FFFF,d0
        beq.s   .main
        move.w  d0,V_RETURN             ; a death scene: the revive clip, then its clip again
        move.w  U_NEXT(a1),d0
        bra     request
.main:  move.w  V_ROUTE,d0
        cmpi.w  #$FFFF,d0
        bne     request
        move.w  U_NEXT(a1),d0
        bra     request

        even
        even
script: incbin  "build/SCRIPT.BIN"      ; test input: u16 unit, frame, buttons, 0 ... $FFFF
        even
pause_spr:
        incbin  "build/PAUSE.SPR"       ; the PAUSE overlay sprites (scd_pack.py MENUS)
        even
menu_tab:
        incbin  "build/MENU.BIN"        ; save menu buttons: action, label colour (build_disc.py menu_files)
        even
        even
name_main:      dc.b    "MAIN.BIN",0
name_beeps:     dc.b    "BEEPS.BIN",0
name_logic:     dc.b    "LOGIC.BIN",0
name_title:     dc.b    "TITLE.BIN",0
name_cheats:    dc.b    "CHEATS.BIN",0
        even
name_save:      dc.b    "BRAINDEAD13",0         ; backup RAM file name (11 characters), then for BRMWRITE:
        dc.w    2                       ; ... flag byte 0 (the end of the name), 2 blocks of 64 bytes

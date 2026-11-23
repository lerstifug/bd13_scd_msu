; PAYLOAD.BIN - main 68000 video player (stream format version 5), assembled for Work RAM at $FF1000.
; The stub in MAIN.BIN (run from Word RAM at $200000) copies this here and jumps to it, so Word RAM can be
; handed back and forth with the sub CPU.
;
; The sub CPU does all the work of building each frame (see app.s); this side is a DMA copier.  Every vertical
; blank it gives Word RAM to the sub CPU, interrupts it, waits for Word RAM to come back and reads the status word:
;   0  nothing new
;   1  frame: DMA its tiles to the idle pool and the tiles of any new plane rows, then in the next vertical blank DMA
;      both plane maps (32 x 32) and set the vertical scroll
;   2  scene change: display off, palette, DMA the scene tiles, border sprites, then the frame as above
;   4  menu screen (H40, the 3DO width): display off, H40 with 64 x 32 planes (sprite table $FC00, hscroll
;      $F800), palette, its tiles from VRAM tile 1, both maps; the next status 2 sets H32 back up
;   3  overlay (pause): DMA a few tiles and add sprites after the border sprites, and/or upload the palette
;      (the save menu selection), or restart the console (the save menu Exit)
;
; VRAM: tile 0 blank | scene tiles 1-R1 | frame pools R1+1.. and R1+1+P..1535 (P = pool size per scene: 410 gives
;       R1 = 715) | plane A $C000 | scene tiles 1600-1791 | plane B $E000 | scene tiles 1856-2015 | hscroll $FC00 |
;       border tiles $FD00 | sprites $FE00
; The 224x192 window is screen lines 16-207, columns 16-239 (static scenes: plane rows 2-25, columns 2-29).
; If the sub CPU never answers the backdrop turns red.
;
; DMA_FIX (default 1): DMA from Word RAM delivers each word one transfer late, so the source is given +2 and the
; first word is written by the CPU afterwards.

        ifnd    DMA_FIX
DMA_FIX equ     1
        endif
; DEBUG=1: the border shows the sub CPU buffer level (read it back from a capture with buflevel.py)
        ifnd    DEBUG
DEBUG   equ     0
        endif

VDPDATA equ     $C00000
VDPCTRL equ     $C00004
WORDRAM equ     $200000
GA_CTL  equ     $A12000                 ; byte: bit0 = IFL2 (level 2 interrupt to the sub CPU)
GA_COMM0 equ    $A12010                 ; word: main -> sub comm (the sub CPU reads $FF8010)
PAD1    equ     $A10003                 ; controller port 1 data / control
PAD1CTL equ     $A10009
GA_MEM  equ     $A12003                ; byte: bit1 = DMNA (give Word RAM to sub), bit0 = RET (1: main has Word RAM)
WR_KF   equ     2                       ; Word RAM layout: see app.s
WR_POOL equ     4
WR_KS   equ     6
WR_VS   equ     8
WR_MASK equ     $A
WR_NRUN equ     $C
WR_FTO  equ     $E
WR_PAL  equ     $10
WR_RUNS equ     $1110
WR_MAPA equ     $100
WR_MAPB equ     $900
WR_DBG  equ     $1100
WR_R1   equ     $B0
WR_RT   equ     $15600
WR_ST   equ     $B400
MASKT   equ     2024                    ; 8 tiles of the border colour at $FD00 (after the hscroll entry)
V_CNT   equ     $FF0F00                 ; work RAM scratch word
V_MASKON equ    $FF0F02                 ; word: the scene has border sprites (sprites 0-13)
V_WIDE  equ     $FF0F04                 ; word: H40 (a menu screen) is set up
WR_WMAPA equ    $18000                  ; menu screens: 64 x 32 plane maps (see app.s)
WR_WMAPB equ    $19000
WR_X    equ     $1E000                  ; status 3, overlay (see app.s): flags, tiles, VRAM tile, sprites, list
X_F     equ     0
X_N     equ     2
X_V     equ     4
X_S     equ     6
X_SPR   equ     8
X_TILES equ     $100

        org     $FF1000

player:
        move.w  #$2700,sr
        lea     VDPDATA,a0
        lea     VDPCTRL,a1

        lea     regs(pc),a2
        moveq   #(regs_end-regs)/2-1,d0
.reg:   move.w  (a2)+,(a1)
        dbra    d0,.reg

        move.l  #$40000000,(a1)         ; clear VRAM
        move.w  #$7FFF,d0
        moveq   #0,d1
.clr:   move.w  d1,(a0)
        dbra    d0,.clr
        move.l  #$40000010,(a1)         ; clear vertical scroll RAM
        move.l  d1,(a0)
        move.l  #$C0000000,(a1)         ; clear CRAM
        moveq   #63,d0
.cc:    move.w  d1,(a0)
        dbra    d0,.cc
        move.w  #$8154,(a1)             ; display on (the vertical blank flag only toggles while it is on)
        move.b  #$40,PAD1CTL.l          ; controller port 1: TH is an output
        clr.w   V_WIDE
        clr.w   V_MASKON

mainloop:
        bsr     waitvb
        bsr     readpad
        move.w  d0,GA_COMM0.l           ; buttons for the sub CPU (it runs the game logic)
        bset    #1,GA_MEM.l             ; give Word RAM to the sub CPU
        bset    #0,GA_CTL.l             ; interrupt the sub CPU: "next frame, please"
        move.l  #200000,d0
.w0:    btst    #0,GA_MEM.l             ; wait for Word RAM to leave us ...
        beq.s   .w1s
        subq.l  #1,d0
        bne.s   .w0
.w1s:   move.l  #3000000,d0
.w1:    btst    #0,GA_MEM.l             ; ... and to come back
        bne.s   gotit
        subq.l  #1,d0
        bne.s   .w1
        bra     fail

gotit:  lea     WORDRAM,a5
        if      DEBUG
        move.l  #$C0000000,(a1)         ; backdrop (border) colour = sub CPU buffer level
        move.w  WR_DBG(a5),(a0)
        endif
        move.w  (a5),d7
        beq.s   mainloop                ; nothing new this vertical blank
        cmpi.w  #3,d7
        beq     overlay
        cmpi.w  #4,d7
        beq     wide
        cmpi.w  #2,d7
        bne     .frame
        bsr     narrow                  ; back from a menu screen: H32 again

        move.w  #$8114,(a1)             ; scene change: display off while uploading
        lea     WR_PAL(a5),a2
        move.l  #$C0000000,(a1)
        moveq   #63,d0
.pal:   move.w  (a2)+,(a0)
        dbra    d0,.pal
        moveq   #1,d6                   ; scene tiles 1.. (split over the three VRAM ranges)
        moveq   #0,d5
        move.w  WR_KS(a5),d5
        lea     WR_ST(a5),a2
        bsr     sdma

        move.w  WR_MASK(a5),d0          ; scrolling scene: 14 sprites in the border colour cover the 16-line
        move.w  d0,V_MASKON             ; bands above and below the window
        beq.s   .nomask
        moveq   #15,d1
        and.w   d0,d1
        move.w  d1,d2
        lsl.w   #4,d2
        or.w    d1,d2                   ; two pixels
        move.w  d2,d1
        lsl.w   #8,d1
        or.w    d1,d2                   ; four
        move.w  d2,d1
        swap    d2
        move.w  d1,d2                   ; eight: one tile row
        move.l  #$7D000003,(a1)         ; VRAM $FD00 = tile MASKT
        moveq   #63,d3
.mf:    move.l  d2,(a0)
        dbra    d3,.mf
        move.w  d0,d1
        lsr.w   #8,d1
        andi.w  #3,d1
        ror.w   #3,d1                   ; palette line -> bits 13-14
        ori.w   #$8000|MASKT,d1         ; priority: in front of both planes
        move.l  #$7E000003,(a1)         ; sprite table at $FE00
        moveq   #0,d3
.sp:    move.w  #128,d4                 ; sprites 0-6: screen lines 0-15, 7-13: lines 208-223
        cmpi.w  #7,d3
        blo.s   .top
        move.w  #128+208,d4
.top:   move.w  d4,(a0)
        move.w  d3,d4
        addq.w  #1,d4                   ; link to the next sprite (the last links to 0)
        cmpi.w  #14,d4
        blo.s   .lk
        moveq   #0,d4
.lk:    ori.w   #$0D00,d4               ; 4 x 2 cells (32 x 16)
        move.w  d4,(a0)
        move.w  d1,(a0)
        move.w  d3,d4
        cmpi.w  #7,d4
        blo.s   .xm
        subq.w  #7,d4
.xm:    lsl.w   #5,d4
        addi.w  #128+16,d4              ; columns 16-239, the window width
        move.w  d4,(a0)
        addq.w  #1,d3
        cmpi.w  #14,d3
        blo.s   .sp
        bra.s   .frame
.nomask:
        move.l  #$7E000003,(a1)         ; no border sprites: sprite 0 off screen, end of the list
        move.l  #0,(a0)
        move.l  #0,(a0)

.frame: moveq   #0,d1
        move.w  WR_KF(a5),d1
        beq.s   .nt
        lsl.w   #4,d1                   ; words
        moveq   #0,d0
        move.w  WR_FTO(a5),d0
        add.l   a5,d0                   ; the tile buffer of this frame
        moveq   #0,d2
        move.w  WR_POOL(a5),d2
        lsl.l   #5,d2
        bsr     dmavram                 ; into the pool that is not on screen
.nt:    lea     WR_RT(a5),a2            ; tiles of the plane rows coming into view (they are off screen now)
        lea     WR_RUNS(a5),a4
        move.w  WR_NRUN(a5),V_CNT
        bra.s   .rc
.rl:    moveq   #0,d6
        move.w  (a4)+,d6
        moveq   #0,d5
        move.w  (a4)+,d5
        bsr     sdma
.rc:    subq.w  #1,V_CNT
        bpl.s   .rl
        cmpi.w  #2,d7
        beq.s   .maps
        bsr     waitvb                  ; switch the maps and the scroll during the vertical blank
.maps:  lea     WR_MAPA(a5),a2
        move.l  a2,d0
        move.w  #32*32,d1
        move.l  #$C000,d2
        bsr     dmavram
        lea     WR_MAPB(a5),a2
        move.l  a2,d0
        move.w  #32*32,d1
        move.l  #$E000,d2
        bsr     dmavram
        move.l  #$40000010,(a1)         ; vertical scroll, plane A then plane B
        move.w  WR_VS(a5),(a0)
        move.w  WR_VS(a5),(a0)
        move.w  #$8154,(a1)             ; display on
        bra     mainloop

; status 3: the overlay block in Word RAM (WR_X) says what to do
overlay:
        lea     WORDRAM+WR_X,a4
        move.w  X_F(a4),d6
        btst    #2,d6
        bne     reboot
        btst    #1,d6
        beq.s   .np
        lea     WR_PAL(a5),a2           ; palette
        move.l  #$C0000000,(a1)
        moveq   #63,d0
.pal:   move.w  (a2)+,(a0)
        dbra    d0,.pal
.np:    btst    #0,d6
        beq     mainloop
        moveq   #0,d1
        move.w  X_N(a4),d1              ; tiles
        beq.s   .nt
        lsl.w   #4,d1
        lea     X_TILES(a4),a2
        move.l  a2,d0
        moveq   #0,d2
        move.w  X_V(a4),d2
        lsl.l   #5,d2
        bsr     dmavram
.nt:    lea     WORDRAM+WR_X,a4
        move.w  X_S(a4),d3              ; sprites 16.. (VRAM $FE80, or $FC80 in H40)
        move.l  #$7E800003,(a1)
        tst.w   V_WIDE
        beq.s   .tb
        move.l  #$7C800003,(a1)
.tb:
        lea     X_SPR(a4),a2
        bra.s   .sc
.sl:    move.l  (a2)+,(a0)
        move.l  (a2)+,(a0)
.sc:    dbra    d3,.sl
        moveq   #0,d1                   ; link them after the last sprite of the list
        tst.w   X_S(a4)
        beq.s   .l0
        moveq   #16,d1
.l0:    tst.w   V_WIDE
        beq.s   .l0n
        move.l  #$7C020003,(a1)         ; H40: sprite 0 at $FC00 (off screen): link
        move.w  d1,(a0)
        bra     mainloop
.l0n:   tst.w   V_MASKON
        beq.s   .l1
        move.l  #$7E6A0003,(a1)         ; sprite 13 (the last border sprite): 4 x 2 cells, link
        ori.w   #$0D00,d1
        move.w  d1,(a0)
        bra     mainloop
.l1:    move.l  #$7E020003,(a1)         ; sprite 0 (off screen): link
        move.w  d1,(a0)
        bra     mainloop

; status 4: a menu screen in H40
wide:   move.w  #$8114,(a1)             ; display off
        lea     wregs(pc),a2
        moveq   #3,d0
.rg:    move.w  (a2)+,(a1)
        dbra    d0,.rg
        move.w  #1,V_WIDE
        lea     WR_PAL(a5),a2           ; palette
        move.l  #$C0000000,(a1)
        moveq   #63,d0
.pal:   move.w  (a2)+,(a0)
        dbra    d0,.pal
        moveq   #0,d1
        move.w  WR_KS(a5),d1            ; tiles from VRAM tile 1
        lsl.l   #4,d1
        lea     WR_ST(a5),a2
        move.l  a2,d0
        move.l  #$20,d2
        bsr     dmavram
        lea     WORDRAM+WR_WMAPA,a2
        move.l  a2,d0
        move.w  #64*32,d1
        move.l  #$C000,d2
        bsr     dmavram
        lea     WORDRAM+WR_WMAPB,a2
        move.l  a2,d0
        move.w  #64*32,d1
        move.l  #$E000,d2
        bsr     dmavram
        move.l  #$78000003,(a1)         ; hscroll $F800: 0
        move.l  #0,(a0)
        move.l  #$40000010,(a1)         ; vscroll 0
        move.l  #0,(a0)
        move.l  #$7C000003,(a1)         ; sprite table $FC00: sprite 0 off screen, end of the list
        move.l  #0,(a0)
        move.l  #0,(a0)
        move.w  #$8154,(a1)             ; display on
        bra     mainloop

; back from H40 before a game scene: H32 registers, and the hscroll entry at $FC00 (it held H40 sprites) cleared
narrow: tst.w   V_WIDE
        beq.s   .x
        clr.w   V_WIDE
        lea     nregs(pc),a2
        moveq   #3,d0
.rg:    move.w  (a2)+,(a1)
        dbra    d0,.rg
        move.l  #$7C000003,(a1)
        move.l  #0,(a0)
.x:     rts

wregs:  dc.w    $8C89                   ; R12 H40, Shadow/Highlight on
        dc.w    $9001                   ; R16 planes 64 x 32
        dc.w    $857E                   ; R5  sprite table at $FC00
        dc.w    $8D3E                   ; R13 hscroll table at $F800
nregs:  dc.w    $8C08                   ; (as regs: H32, 32 x 32 planes, sprites $FE00, hscroll $FC00)
        dc.w    $9000
        dc.w    $857F
        dc.w    $8D3F

; the save menu Exit: restart the console through the reset vector (no title screen yet)
reboot: move.w  #$2700,sr
        movea.l $0.w,sp
        movea.l $4.w,a0
        jmp     (a0)

; DMA d5 scene tiles from (a2), starting at scene tile d6, into their VRAM ranges: with R1 = WR_R1 (set by the
; sub CPU from the scene pool size), scene tiles 1..R1 are VRAM tiles 1..R1, the next 192 are 1600-1791 and the
; rest 1856-2015.  a2 advances.  Clobbers d0-d6,a3.
sdma:   tst.l   d5
        beq.s   .x
        moveq   #0,d1
        move.w  WR_R1(a5),d1
        cmp.l   d1,d6
        bhi.s   .r2
        move.l  d6,d2                   ; range 1
        addq.l  #1,d1                   ; it ends before R1+1
        bra.s   .go
.r2:    move.l  d1,d0
        addi.l  #192,d0
        cmp.l   d0,d6
        bhi.s   .r3
        move.l  d6,d2                   ; range 2: 1600 + k - (R1+1)
        sub.l   d1,d2
        addi.l  #1600-1,d2
        move.l  d0,d1
        addq.l  #1,d1                   ; it ends before R1+193
        bra.s   .go
.r3:    move.l  d6,d2                   ; range 3: 1856 + k - (R1+193)
        sub.l   d0,d2
        addi.l  #1856-1,d2
        move.l  #4096,d1
.go:    sub.l   d6,d1                   ; room left in this range
        cmp.l   d5,d1
        bls.s   .n
        move.l  d5,d1
.n:     sub.l   d1,d5
        add.l   d1,d6
        lsl.l   #5,d2                   ; VRAM byte address
        move.l  d1,d3
        lsl.l   #5,d3
        move.l  a2,d0
        adda.l  d3,a2
        lsl.w   #4,d1                   ; tiles -> words
        bsr     dmavram
        bra.s   sdma
.x:     rts

; DMA d1.w words from 68000 address d0 to VRAM byte address d2.  Clobbers d0,d3,d4,a3.
dmavram:
        move.l  d0,d3
        if      DMA_FIX
        addq.l  #2,d0
        endif
        lsr.l   #1,d0
        move.w  #$9300,d4
        move.b  d1,d4
        move.w  d4,(a1)                 ; R19 length low
        move.w  d1,d4
        lsr.w   #8,d4
        ori.w   #$9400,d4
        move.w  d4,(a1)                 ; R20 length high
        move.w  #$9500,d4
        move.b  d0,d4
        move.w  d4,(a1)                 ; R21 source bits 1-8
        lsr.l   #8,d0
        move.w  #$9600,d4
        move.b  d0,d4
        move.w  d4,(a1)                 ; R22 source bits 9-16
        lsr.l   #8,d0
        move.w  #$9700,d4
        move.b  d0,d4
        andi.b  #$7F,d4
        move.w  d4,(a1)                 ; R23 source bits 17-23, 68000 -> VDP
        bsr.s   vramcmd
        ori.w   #$0080,d4               ; CD5: DMA
        move.w  d4,(a1)                 ; starts the DMA; the 68000 waits until it is done
        if      DMA_FIX
        bsr.s   vramcmd
        move.w  d4,(a1)
        movea.l d3,a3
        move.w  (a3),(a0)               ; first word by hand
        endif
        rts

; write the first word of a VRAM write command for address d2 to the VDP; d4 = its second word
vramcmd:
        move.w  d2,d4
        andi.w  #$3FFF,d4
        ori.w   #$4000,d4
        move.w  d4,(a1)
        move.l  d2,d4
        lsr.l   #8,d4
        lsr.l   #6,d4
        andi.w  #3,d4
        rts

; 3-button pad on port 1 -> d0.w: bit 0 up, 1 down, 2 left, 3 right, 4 action (A, B or C), 7 START, 8 A, 9 B,
; 10 C (1 = pressed)
readpad:
        move.b  #$40,PAD1.l             ; TH = 1: C B R L D U
        nop
        nop
        move.b  PAD1.l,d1
        move.b  #$00,PAD1.l             ; TH = 0: START A 0 0 D U
        nop
        nop
        move.b  PAD1.l,d2
        move.b  #$40,PAD1.l
        not.b   d1                      ; the pad reads 0 when pressed
        not.b   d2
        moveq   #$0F,d0
        and.b   d1,d0                   ; directions
        btst    #5,d2
        beq.s   .ns
        bset    #7,d0                   ; START
.ns:    btst    #4,d2
        beq.s   .nA
        bset    #8,d0                   ; A, B and C on their own too (bits 8-10; the cheat menu: B goes back)
.nA:    btst    #4,d1
        beq.s   .nB
        bset    #9,d0
.nB:    btst    #5,d1
        beq.s   .nC
        bset    #10,d0
.nC:    andi.b  #$30,d1                 ; B, C
        andi.b  #$10,d2                 ; A
        or.b    d2,d1
        beq.s   .na
        bset    #4,d0                   ; action
.na:    rts

fail:   move.l  #$C0000000,(a1)         ; backdrop = red: the sub CPU stopped answering
        move.w  #$000E,(a0)
        move.w  #$8154,(a1)
.hang:  bra.s   .hang

waitvb: move.w  (a1),d0
        btst    #3,d0
        bne.s   waitvb                  ; still in the previous vertical blank
.s:     move.w  (a1),d0
        btst    #3,d0
        beq.s   .s                      ; wait for the next one to start
        rts

regs:
        dc.w    $8004                   ; R0  no horizontal interrupt
        dc.w    $8114                   ; R1  display off, DMA on, mode 5
        dc.w    $8230                   ; R2  plane A map at $C000
        dc.w    $8300                   ; R3  window map (unused)
        dc.w    $8407                   ; R4  plane B map at $E000
        dc.w    $857F                   ; R5  sprite table at $FE00
        dc.w    $8700                   ; R7  backdrop = palette 0 colour 0
        dc.w    $8A00                   ; R10 horizontal interrupt counter
        dc.w    $8B00                   ; R11 full-screen scroll
        dc.w    $8C08                   ; R12 H32 (256 wide), Shadow/Highlight on: a cell whose plane A and B
                                        ;     tiles both have priority 0 is shown at half brightness
        dc.w    $8D3F                   ; R13 hscroll table at $FC00
        dc.w    $8F02                   ; R15 auto-increment 2
        dc.w    $9000                   ; R16 planes 32x32
        dc.w    $9100                   ; R17 window H
        dc.w    $9200                   ; R18 window V
regs_end:

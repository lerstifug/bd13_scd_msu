; BOOT.BIN - the boot area of the Brain Dead 13 Sega CD disc: sectors 0-15 (32 KB), assembled by build_disc.py.
;
; Layout as megadev lib/cd_boot.s (github.com/drojaazu/megadev, MIT licence):
;   $0000  volume header (disc type, volume and system names, where the IP and the SP are, the date)
;   $0100  disc header (hardware, copyright, titles, serial, controller, region)
;   $0200  IP, the main CPU initial program: the region security code first (the BIOS checks it byte for byte),
;          then our start-up code.  The security code is not in this file: build_disc.py takes it from
;          lib/security.c of a megadev checkout (tools/megadev) and writes build/SECURITY.BIN.
;   $1000  SP, the sub CPU initial program (the BIOS loads it to $6000): a small loader that starts APP.BIN.
;
; What the player expects (APP.BIN, MAIN.BIN):
;   the sub CPU runs APP.BIN at $8000 with a6 = the loader globals: +0 vertical blank handler (called from the
;   level 2 interrupt with its parameter on the stack), +4 that parameter, +36 LoadFile (a0 = name, a1 = where;
;   returns d0 = 0 with Z set when loaded, else d0 < 0).
;   The main CPU waits until the sub CPU sends M and hands it Word RAM, then runs MAIN.BIN at $200000.
; The SP code is position independent: it runs at $6000 but is assembled at $1000.

BIOS            equ     $5F22           ; sub CPU BIOS call (d0 = function)
DRVINIT         equ     $0010
ROMREADN        equ     $0020
CDCSTOP         equ     $0089
CDCSTAT         equ     $008A
CDCREAD         equ     $008B
CDCTRN          equ     $008C
CDCACK          equ     $008D
CDCSETMODE      equ     $0096

APP             equ     $8000           ; where APP.BIN goes (sub PRG RAM)

GV_VBLANK       equ     0               ; loader globals (the layout APP.BIN uses)
GV_PARAM        equ     4
GV_READ         equ     12
GV_LOAD         equ     36
GV_ROOT         equ     40              ; root directory: first sector, sectors
GV_ROOTN        equ     44
GV_SIZE         equ     48

; ---------------------------------------------------------------------------------------------- volume header

        org     0
        dc.b    "SEGADISCSYSTEM  "
        dc.b    "BRAINDEAD13",0         ; volume name
        dc.w    $0100,$0001             ; volume version, type
        dc.b    "BD13 SYSTEM",0         ; system name
        dc.w    $0100,$0000             ; system version
        dc.l    $800,$800,0,0           ; IP: disc offset, size, entry offset, work RAM
        dc.l    $1000,sp_end-sp_start,0,0       ; SP: the same
        dc.b    "11232026        "      ; date, MMDDYYYY
        dcb.b   $100-*,' '

; ---------------------------------------------------------------------------------------------- disc header

        dc.b    "SEGA GENESIS    "      ; hardware (megadev uses this for the US region)
        dc.b    "(C)     2026.NOV"
        dc.b    "BRAINDEAD 13"          ; title (domestic), 48 characters
        dcb.b   36,' '
        dc.b    "BRAINDEAD 13"          ; title (overseas)
        dcb.b   36,' '
        dc.b    "GM 00000000-00  "      ; serial
        dc.b    "J               "      ; controller: joypad
        dcb.b   $1F0-*,' '
        dc.b    "U               "      ; region: the US security code

; ---------------------------------------------------------------------------------------------- IP (main CPU)

        incbin  "build/SECURITY.BIN"    ; ends by branching to the code after it

ip:     lea     $A10000,a5
        clr.b   $200E(a5)               ; main comm port: nothing yet
.init:  cmp.b   #'I',$200F(a5)          ; the sub CPU loader is running
        bne.s   .init
        bset    #1,$2003(a5)            ; Word RAM to the sub CPU
        move.b  #'B',$200E(a5)          ; boot: load APP.BIN
.app:   cmp.b   #'M',$200F(a5)          ; the player is ready
        bne.s   .app
.wram:  btst    #0,$2003(a5)            ; and has handed Word RAM over (MAIN.BIN is in it)
        beq.s   .wram
        move.w  #$2700,sr
        lea     $FFFD00,sp
        jmp     $200000

        dcb.b   $1000-*,0               ; (an IP past $1000 fails here)

; ---------------------------------------------------------------------------------------------- SP (sub CPU)

sp_start:
        dc.b    "MAIN-SUBCPU",0         ; module name
        dc.w    $0100,$0000             ; version, type
        dc.l    0                       ; next module
        dc.l    sp_end-sp_start         ; module size
        dc.l    sp_jt-sp_start          ; jump table
        dc.l    0                       ; work RAM
sp_jt:  dc.w    sp_init-sp_jt,sp_main-sp_jt,sp_int2-sp_jt,sp_user-sp_jt,0

sp_init:                                ; (before interrupts are on)
        lea     gv(pc),a0
        clr.l   GV_VBLANK(a0)
        clr.l   GV_PARAM(a0)
        lea     read_cd(pc),a1
        move.l  a1,GV_READ(a0)
        lea     load_file(pc),a1
        move.l  a1,GV_LOAD(a0)
        andi.b  #$E2,$FFFF8003.w        ; priority mode off, 2M Word RAM
        move.b  #'I',$FFFF800F.w        ; sub comm port: the loader runs
        rts

sp_main:
        lea     gv(pc),a6
        bsr     cd_init
.boot:  cmp.b   #'B',$FFFF800E.w        ; wait for the main CPU
        bne.s   .boot
        clr.b   $FFFF800F.w
        lea     gv(pc),a6
        lea     app_name(pc),a0
        lea     APP.l,a1                ; (.l: vasm would make it PC relative, wrong at $6000)
        bsr     load_file
        bne.s   .stop                   ; (no APP.BIN: nothing to run)
        lea     gv(pc),a6
        jsr     APP.l
.stop:  bra.s   .stop

sp_int2:                                ; vertical blank: the handler of the player, when it has set one
        lea     gv(pc),a0               ; (as cdboot: only a0 is used until the player sets a handler)
        tst.l   GV_VBLANK(a0)
        beq.s   .none
        move.l  GV_PARAM(a0),-(sp)
        movea.l GV_VBLANK(a0),a0
        jsr     (a0)
        addq.l  #4,sp
.none:  rts

sp_user:
        rts

; cd_init: start the drive, read the primary volume descriptor (sector 16): the root directory.
cd_init:
        lea     drive_parms(pc),a0
        move.w  #DRVINIT,d0
        jsr     BIOS.w
        moveq   #0,d1                   ; mode 1 (CD-ROM data, error corrected)
        move.w  #CDCSETMODE,d0
        jsr     BIOS.w
        moveq   #16,d0
        moveq   #1,d1
        lea     buffer(pc),a0
        bsr     read_cd
        lea     buffer(pc),a0
        lea     gv(pc),a1
        move.l  156+6(a0),GV_ROOT(a1)   ; root directory record: first sector (big-endian half)
        move.l  156+14(a0),d0           ; and length in bytes
        add.l   #2047,d0
        moveq   #11,d1
        lsr.l   d1,d0
        move.l  d0,GV_ROOTN(a1)
        rts

; load_file: a0 = name (as on the disc without ;1, ending with 0), a1 = where.  d0 = 0 (Z) when loaded, else -1.
; Looks in the root directory only.
load_file:
        movem.l d2-d7/a2-a5,-(sp)
        movea.l a0,a4                   ; a4 name, a5 where
        movea.l a1,a5
        lea     gv(pc),a3
        move.l  GV_ROOT(a3),d6          ; d6 directory sector, d7 sectors left
        move.l  GV_ROOTN(a3),d7
.sector:
        tst.l   d7
        beq.s   .missing
        move.l  d6,d0
        moveq   #1,d1
        lea     buffer(pc),a0
        bsr     read_cd
        lea     buffer(pc),a2           ; a2 record
        lea     2048(a2),a3             ; a3 end of the sector
.record:
        cmpa.l  a3,a2
        bhs.s   .next
        moveq   #0,d2
        move.b  (a2),d2                 ; record length (0: no more records in this sector)
        beq.s   .next
        moveq   #0,d3
        move.b  32(a2),d3               ; name length
        lea     33(a2),a0
        movea.l a4,a1
.cmp:   tst.b   (a1)
        beq.s   .end                    ; our name ended: the disc name must end too (or go on with ;)
        subq.w  #1,d3
        bmi.s   .skip
        cmpm.b  (a0)+,(a1)+
        beq.s   .cmp
        bra.s   .skip
.end:   tst.w   d3
        beq.s   .found
        cmp.b   #$3B,(a0)               ; (a semicolon: the version, ;1)
        beq.s   .found
.skip:  adda.l  d2,a2
        bra.s   .record
.next:  addq.l  #1,d6
        subq.l  #1,d7
        bra.s   .sector
.found: move.l  6(a2),d0                ; first sector (big-endian half)
        move.l  14(a2),d1               ; length in bytes
        add.l   #2047,d1
        moveq   #11,d2
        lsr.l   d2,d1
        movea.l a5,a0
        bsr     read_cd
        movem.l (sp)+,d2-d7/a2-a5
        moveq   #0,d0
        rts
.missing:
        movem.l (sp)+,d2-d7/a2-a5
        moveq   #-1,d0
        rts

; read_cd: d0 = first sector, d1 = sectors, a0 = where.  Reads them with the sub CPU (BIOS ROMREADN, then per
; sector CDCSTAT, CDCREAD, CDCTRN, CDCACK, as megadev lib/sub/cdrom.s load_data_sub); a sector that fails starts
; the read again from it.  The ROMREADN block (first sector, sectors) is on the stack, as cdboot has it: with the
; block in the memory of the loader itself no sector ever came (Kega Fusion, 2026-11-23).
read_cd:
        movem.l d0-d1/a0-a1,-(sp)       ; 0 first sector, 4 sectors, 8 where, 12 the sector header (CDCTRN)
.again: move.w  #CDCSTOP,d0
        jsr     BIOS.w
        movea.l sp,a0
        move.w  #ROMREADN,d0
        jsr     BIOS.w
.sector:
        move.w  #CDCSTAT,d0             ; wait for a sector in the CDC buffer
        jsr     BIOS.w
        bcs.s   .sector
        andi.w  #$F8FF,$FFFF8004.w      ; CDC destination: sub CPU read
        ori.w   #$0300,$FFFF8004.w
.read:  move.w  #CDCREAD,d0
        jsr     BIOS.w
        bcs.s   .read
        movea.l 8(sp),a0
        lea     12(sp),a1
        move.w  #CDCTRN,d0
        jsr     BIOS.w
        bcs.s   .again
        move.w  #CDCACK,d0
        jsr     BIOS.w
        addq.l  #1,(sp)
        add.l   #2048,8(sp)
        subq.l  #1,4(sp)
        bne.s   .sector
        lea     16(sp),sp
        moveq   #0,d0
        rts

drive_parms:
        dc.b    1,$FF                   ; first track, last track (all)
app_name:
        dc.b    "APP.BIN",0
        cnop    0,4
gv:     dcb.b   GV_SIZE,0
buffer: dcb.b   2048,0                  ; directory and volume descriptor sector
sp_end:

        dcb.b   $8000-*,0

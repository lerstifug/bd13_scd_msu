; Brain Dead 13 SNES MSU-1 player (WLA-DX wla-65816).
;
; Plays the game from the MSU-1 data file (<rom>.msu): units (a clip's main path or one death scene; units.inc, made
; by pack_game.py, gives each one's offset, audio track <rom>-<track>.pcm, what follows it, and its decisions, in
; fields).  The rules are the Sega CD player's:
;   death decision: in its window the first move decides; the right one plays on, a wrong one (or none by the window's
;     end) starts the death scene at once; after it the revive clip, then this clip again.
;   route decision: every move in its window chooses again (the last one wins; one it does not offer drops the
;     choice), and the way chosen takes over at the decision's switch field.  With none the clip plays on, usually into a death for waiting.  An outcome can be a list, one picked at
;     random: the deaths of a wrong way, which come back here after their revive clip.
;   COND rules: once all the clips of a rule have been visited, a route to its target goes to its replacement.
; The encoder does all the video work: a unit is one packet per 60 Hz field, and the player just runs each packet
; in the lines outside the picture (220 to 3 of the next field, forced blank), so a packet is a list of DMAs
; straight from the MSU-1 data port ($2001) to VRAM or CGRAM.
;   packet: flags (bit0: stay blank through the next field, bit7: end of the unit), command count, commands
;   command 0: VRAM word address (2), byte count (2), data      command 1: CGRAM address (1), byte count (2), data
;   command 2: as 0, stepping 32 words (a map column)           command 3: BG2 scroll (2 horizontal, 2 vertical)
;   command 4: PPU register $21xx (1), value (1): the alternating buffers of heavy still scenes
; Mode 1: BG1 = cel layer (tiles from VRAM word $4000, 32x32 map at $7C00), BG2 = background (tiles from 0,
; 64x32 map at $7400).

.MEMORYMAP
  SLOTSIZE $8000
  DEFAULTSLOT 0
  SLOT 0 $8000
.ENDME

.ROMBANKSIZE $8000
.ROMBANKS 4

.SNESHEADER
  ID "SNES"
  NAME "BRAIN DEAD 13 MSU1   "
  SLOWROM
  LOROM
  CARTRIDGETYPE $02             ; ROM, RAM, battery: the saved game
  ROMSIZE $07
  SRAMSIZE $01                  ; 2 KB
  COUNTRY $01
  LICENSEECODE $00
  VERSION $00
.ENDSNES

.SNESNATIVEVECTOR
  COP Halt
  BRK Halt
  ABORT Halt
  NMI Halt
  UNUSED Halt
  IRQ Halt
.ENDNATIVEVECTOR

.SNESEMUVECTOR
  COP Halt
  UNUSED Halt
  ABORT Halt
  NMI Halt
  RESET Start
  IRQBRK Halt
.ENDEMUVECTOR

; direct page variables
.DEFINE FLAGS   $00
.DEFINE COUNT   $01
.DEFINE VLINE   $10             ; word: the current scanline
.DEFINE UNIT    $12             ; word: the unit playing
.DEFINE UPTR    $14             ; word: its entry in UnitTable
.DEFINE PK      $16             ; word: its packet (field) count
.DEFINE DEC     $18             ; word: its next decision
.DEFINE MOVES   $1A             ; the moves held: bit 0 up, 1 down, 2 left, 3 right, 4 fire (outcome = bit + 1), 5 start
.DEFINE PREV    $1B             ; the moves held in the field before
.DEFINE NEW     $1C             ; the moves pressed in this field
.DEFINE TMP     $1E             ; word
.DEFINE SCRIPT  $20             ; word: the next test press
.DEFINE ROUTE   $22             ; word: the route chosen in this unit ($FFFF: none)
.DEFINE CUT     $24             ; word: the field it takes over
.DEFINE RETURN  $26             ; word: the unit to go back to after a revive clip
.DEFINE RAND    $28             ; word: random number state, stepped every field
.DEFINE TMP2    $2A             ; word
.DEFINE RULES   $2C             ; word (RouteOut)
.DEFINE TARGET  $2E             ; word (RouteOut)
.DEFINE REPL    $30             ; word (RouteOut)
.DEFINE LEFT    $32             ; word (RouteOut)
.DEFINE MISSING $34             ; word (RouteOut)
.DEFINE TSEL    $36             ; word: the start screen's selected button
.DEFINE BTAB    $38             ; word: the menu showing (TitleButtons or MenuButtons)
.DEFINE BLINK   $3A             ; word: fields since the PAUSE text last blinked
.DEFINE RESUME  $3C             ; word: the clip to play after a pause
.DEFINE WANTPAUSE $3E           ; START was pressed in a field with no picture: pause when there is one
.DEFINE TM      $3F             ; what the main screen shows ($212C)
.DEFINE VISIT   $40             ; 64 bytes: a bit per unit that has been played (COND rules, saves)
.DEFINE PAGE    $80             ; word: the page of BG1 tiles lending slots to the PAUSE text ($FFFF: none has room)
.DEFINE FLASH   $82             ; word: fields until the Save label takes its colour back
.DEFINE PADDR   $84             ; word: the VRAM address of the page of the text
.DEFINE POBJ    $86             ; its sprite tile base ($2101)
.DEFINE OBANK   $88             ; the ROM bank of the showing menu's OAM images (the cheat menu's are in bank 1)
.DEFINE CODE    $89             ; the start screen: the cheat code entered so far (Up Down Up Down Up Down Up Down, Left)
.DEFINE BEEPN   $8A             ; the last value sent to sound port 0 (a new one plays a beep)
.DEFINE HOLDSCR $8B             ; nonzero: the press list waits (paused in a cheat map: its presses are for the clip)
.DEFINE CHEATON $8C             ; nonzero: a cheat map is playing (its press list is SCRIPT)
.DEFINE RINGI   $8E             ; word: the newest entry in RING
.DEFINE RSCRIPT $90             ; word: where the press list goes back to when a pause resumes
; work RAM (pause)
.DEFINE MAPBUF  $0200           ; 2 KB: BG1 map
.DEFINE USED    $0A00           ; 1 KB: per BG1 tile, 1 if the picture on screen shows it
.DEFINE OAMBUF  $0E00           ; 544 bytes: the PAUSE sprites
.DEFINE FREE    $1020           ; 256 bytes: the slots found
.DEFINE HDMA_RG $1200           ; 768 bytes: HDMA table, the gradient red and green (channel 6)
.DEFINE HDMA_B  $1500           ; 512 bytes: HDMA table, the gradient blue (channel 7)
.DEFINE RING    $1800           ; 64 bytes: the last 16 units started, each with where the press list was (unit, SCRIPT)
; the saved game (cartridge RAM): BD13, build id, clip, visited units
.DEFINE SAVE    $700000
.DEFINE LABEL_COLOUR $7FE0      ; the save menu: the labels own colour in the 3DO cels
.DEFINE SAVED_COLOUR $03E0      ; ... and the Save label for a moment after saving

; UnitTable entries (units.inc, 16 bytes): offset (4), then words: decisions, track, next unit, unit to restart after
; the revive clip ($FFFF: none), COND rules (0: none); then bytes: decision count, flags
.DEFINE U_DEC     4
.DEFINE U_TRACK   6
.DEFINE U_NEXT    8
.DEFINE U_RESTART 10
.DEFINE U_CONDS   12
.DEFINE U_NDEC    14
.DEFINE U_FLAGS   15
.DEFINE F_REVIVE    $01         ; a revive clip: after it, back to RETURN
.DEFINE F_MENU      $02         ; the save menu: no sound, and when its picture is on screen, MenuScreen
.DEFINE F_DEATHFILE $04         ; a death clip of its own: a route to it returns to the clip that chose it
.DEFINE F_SKIP      $10         ; the intro and the trailers: any button goes on to SKIP_UNIT
.DEFINE F_TITLE     $20         ; the start screen: no sound, and when its picture is on screen, TitleScreen
.DEFINE F_CHEAT     $40         ; the cheat menu: no sound, and when its picture is on screen, CheatScreen
; TitleButtons entries (16 bytes, all words): action (0 New Game, 1 Continue, 2 plays its unit), unit, the button that
; way (up, down, left, right; $FFFF: none), its OAM image, 0.  MenuButtons the same: action 0 Continue, 1 Save, 2 Exit.
; CheatButtons the same: action 3, the map's first clip, its OAM image and press list (both in bank 1)
.DEFINE B_ACTION 0
.DEFINE B_UNIT   2
.DEFINE B_DIRS   4
.DEFINE B_OAM    12
.DEFINE B_SCRIPT 14
; decisions (20 bytes, all words): first field, end field, switch field, kind (0 death, 1 route), an outcome for each
; input: none, up, down, left, right, fire.  An outcome is a unit, $FFFF (death: the right move, play on; route: not
; offered), or above $8000 a pointer to a list: count, units
.DEFINE D_START 0
.DEFINE D_END   2
.DEFINE D_CUT   4
.DEFINE D_KIND  6
.DEFINE D_OUT   8
; COND rules: count, then per rule: target, replacement ($FFFF: no longer offered), count, units

; MSU-1 registers
.DEFINE MSU_STATUS  $2000
.DEFINE MSU_READ    $2001
.DEFINE MSU_SEEK    $2000
.DEFINE MSU_TRACK   $2004
.DEFINE MSU_VOLUME  $2006
.DEFINE MSU_CONTROL $2007

.DEFINE LINE_BLANK  220         ; the first line under the picture (216 lines: the 3DO's height)
.DEFINE LINE_SHOW   4           ; the first picture line

.BANK 0 SLOT 0
.ORG 0
.SECTION "Player" FORCE

Halt:
  rti

Start:
  sei
  clc
  xce                           ; native mode
  rep #$38                      ; 16-bit A/X/Y, binary mode
  .ACCU 16
  .INDEX 16
  ldx.w #$1FFF
  txs
  lda.w #$0000
  tcd                           ; direct page 0
  sep #$20
  .ACCU 8
  pha
  plb                           ; data bank 0

  lda.b #$80
  sta.w $2100                   ; forced blank
  stz.w $4200                   ; no NMI, no IRQ, no joypad auto-read
  lda.b #$FF
  sta.w $4201                   ; bit 7 set: reading $2137 latches the H/V counters
  stz.w $420B
  stz.w $420C                   ; no HDMA

  ; PPU setup
  lda.b #$03
  sta.w $2101                   ; sprites (the start screen's outline): 8x8, tiles from word $6000
  lda.b #$01
  sta.w $2105                   ; mode 1, 8x8 tiles
  stz.w $2106                   ; no mosaic
  lda.b #$7C
  sta.w $2107                   ; BG1 map at word $7C00, 32x32
  lda.b #$75
  sta.w $2108                   ; BG2 map at word $7400, 64x32
  stz.w $2109
  stz.w $210A
  lda.b #$04
  sta.w $210B                   ; BG1 tiles at word $4000, BG2 tiles at 0
  stz.w $210C
  stz.w $210D                   ; BG1 horizontal scroll 0
  stz.w $210D
  stz.w $210F                   ; BG2 horizontal scroll 0
  stz.w $210F
  lda.b #$04                    ; vertical scroll 4: screen line 4 (the picture's top) shows map line 8 (row 1)
  sta.w $210E
  stz.w $210E
  lda.b #$04
  sta.w $2110
  stz.w $2110
  lda.b #$33                    ; window 1, outside: BG1 and BG2 masked left and right of the picture
  sta.w $2123
  stz.w $2124
  lda.b #$20                    ; the colour window is window 1 (the picture)
  sta.w $2125
  lda.b #$02
  sta.w $2126                   ; window 1: x 2-253, the 252-pixel picture (the 3DO's 288 at 8:7)
  lda.b #$FD
  sta.w $2127
  stz.w $2128
  stz.w $2129
  stz.w $212A
  stz.w $212B
  lda.b #$03
  sta.w $212C                   ; main screen: BG1 and BG2
  stz.w $212D
  lda.b #$03
  sta.w $212E                   ; main screen window masking on BG1 and BG2
  stz.w $212F
  lda.b #$50                    ; colour math inside the picture only, black outside it (a gradient: the
  sta.w $2130                   ; backdrop plus the fixed colour; no layer has colour math until then)
  stz.w $2131
  lda.b #$E0
  sta.w $2132                   ; fixed colour black
  stz.w $2133

  ; CGRAM entry 0 (the backdrop) black until the stream sets the palettes
  stz.w $2121
  stz.w $2122
  stz.w $2122

  lda.b #$FF
  sta.w MSU_VOLUME
  jsr SpcUpload                 ; the button beeps on the sound chip
  lda.b #$01
  sta.w $4200                   ; joypad auto-read on (no NMI, no IRQ)
  stz.b PREV
  stz.b HOLDSCR
  stz.b CHEATON
  ldx.w #$FFFF
  stx.w RING                    ; (the ring: no unit; StartUnit fills it)
  ldx.w #2
-
  lda.b #$FF
  sta.w RING,x
  inx
  cpx.w #64
  bne -
  ldx.w #0
  stx.b RINGI
  ldx.w #Script
  stx.b SCRIPT
  stz.b WANTPAUSE
  lda.b #$03
  sta.b TM
  ldx.w #$FFFF
  stx.b RETURN
  ldx.w #$ACE1
  stx.b RAND
  ldx.w #63
-
  stz.b VISIT,x                 ; nothing visited yet
  dex
  bpl -
  jsr WaitBlank
  ldx.w #START_UNIT
  jsr StartUnit                 ; its first packet runs in this field
  bra Packet

Next:
  jsr WaitBlank
  jsr Input
  jsr PauseCheck                ; (does not come back if it pauses)
  jsr Decide                    ; (the next unit may start here)
Packet:
  lda.w MSU_READ
  sta.b FLAGS
  bpl +
  jsr UnitEnd                   ; the unit ended: the one after it, its first packet in this field
  bra Packet
+
  lda.w MSU_READ
  sta.b COUNT
  bne Command
  jmp Done
Command:
  lda.w MSU_READ
  beq Vram
  cmp.b #$01
  beq Palette
  cmp.b #$02
  beq Column
  cmp.b #$03
  beq Scroll
  cmp.b #$05
  bne +
  jmp Gradient
+
  lda.w MSU_READ                ; 4: a PPU register $21xx (BG2's map, the main screen layers)
  rep #$20
  .ACCU 16
  and.w #$00FF
  tax
  sep #$20
  .ACCU 8
  lda.w MSU_READ
  sta.w $2100,x
  cpx.w #$002C
  bne Counted
  sta.b TM
  bra Counted
Scroll:
  lda.w MSU_READ                ; 3: BG2 scroll, horizontal then vertical
  sta.w $210F
  lda.w MSU_READ
  sta.w $210F
  lda.w MSU_READ
  sta.w $2110
  lda.w MSU_READ
  sta.w $2110
  jmp Counted
Column:
  lda.b #$81                    ; VRAM: word writes, step 32 words after the high byte
  bra VramStep
Vram:
  lda.b #$80                    ; VRAM: word writes, step 1 word after the high byte
VramStep:
  sta.w $2115
  lda.w MSU_READ
  sta.w $2116
  lda.w MSU_READ
  sta.w $2117
  lda.b #$09                    ; A to B, fixed source address, mode 1 ($2118/$2119)
  sta.w $4300
  lda.b #$18
  sta.w $4301
  bra Count
Palette:
  lda.w MSU_READ
  sta.w $2121
  lda.b #$08                    ; A to B, fixed source address, mode 0 ($2122)
  sta.w $4300
  lda.b #$22
  sta.w $4301
Count:
  lda.w MSU_READ
  sta.w $4305
  lda.w MSU_READ
  sta.w $4306
  ldx.w #MSU_READ
  stx.w $4302
  stz.w $4304
  lda.b #$01
  sta.w $420B                   ; DMA channel 0: the CPU waits until it is done
Counted:
  dec.b COUNT
  beq Done
  jmp Command
Done:
  rep #$20
  .ACCU 16
  inc.b PK
  sep #$20
  .ACCU 8
  lda.b FLAGS
  and.b #$01
  bne +                         ; a blank field: stay blank
  jsr Show
+
  jmp Next

; ---- 5: the backdrop gradient: two HDMA tables (lengths first), copied to work RAM; HDMA writes the fixed colour
; ($2132) on every line, channel 6 red and green, channel 7 blue, and colour math adds it to the backdrop.  No
; tables: the gradient off
Gradient:
  stz.w $420C                   ; HDMA off while the tables change
  lda.w MSU_READ
  sta.b TMP
  lda.w MSU_READ
  sta.b TMP+1                   ; the red and green table length
  lda.w MSU_READ
  sta.b TMP2
  lda.w MSU_READ
  sta.b TMP2+1                  ; the blue table length
  rep #$20
  .ACCU 16
  lda.b TMP
  ora.b TMP2
  sep #$20
  .ACCU 8
  bne +
  jsr GradientOff
  jmp Counted
+
  ldx.w #HDMA_RG
  ldy.b TMP
  jsr ToWram
  ldx.w #HDMA_B
  ldy.b TMP2
  jsr ToWram
  lda.b #$02                    ; channel 6: direct, $2132 written twice
  sta.w $4360
  lda.b #$32
  sta.w $4361
  ldx.w #HDMA_RG
  stx.w $4362
  stz.w $4364
  stz.w $4370                   ; channel 7: direct, $2132 once
  lda.b #$32
  sta.w $4371
  ldx.w #HDMA_B
  stx.w $4372
  stz.w $4374
  lda.b #$20
  sta.w $2131                   ; colour math: add the fixed colour to the backdrop
  lda.b #$C0
  sta.w $420C                   ; HDMA channels 6 and 7 (from the next field)
  jmp Counted

; ---- the gradient off: no HDMA, no colour math, fixed colour black
GradientOff:
  stz.w $420C
  stz.w $2131
  lda.b #$E0
  sta.w $2132
  rts

; ---- Y bytes from the MSU data port to work RAM X (bank $7E), by DMA channel 0
ToWram:
  stx.w $2181
  stz.w $2183
  lda.b #$08                    ; A to B, fixed source address, mode 0 ($2180)
  sta.w $4300
  lda.b #$80
  sta.w $4301
  ldx.w #MSU_READ
  stx.w $4302
  stz.w $4304
  sty.w $4305
  lda.b #$01
  sta.w $420B
  rts

; ---- the unit ended: a route chosen whose switch field is past the end; a revive clip goes back to where the death
; happened; a death scene goes on to the revive clip and then its clip again; a main path goes to its next unit
UnitEnd:
  ldx.b UPTR
  lda.w U_FLAGS,x
  and.b #F_TITLE
  beq +
  plx                           ; (it does not come back here)
  jmp TitleScreen
+
  lda.w U_FLAGS,x
  and.b #F_MENU
  beq +
  plx                           ; (it does not come back here)
  jmp MenuScreen
+
  lda.w U_FLAGS,x
  and.b #F_CHEAT
  beq +
  plx                           ; (it does not come back here)
  jmp CheatScreen
+
  ldx.b ROUTE
  cpx.w #$FFFF
  beq +
  jmp TakeRoute
+
  ldx.b UPTR
  lda.w U_FLAGS,x
  and.b #F_REVIVE
  beq +
  ldx.b RETURN
  cpx.w #$FFFF
  bne ++
  ldx.w #START_UNIT
++
  jmp StartUnit
+
  ldy.w U_RESTART,x
  cpy.w #$FFFF
  beq +
  sty.b RETURN
+
  ldy.w U_NEXT,x
  tyx
  jmp StartUnit

; ---- go to route X: a death clip of its own returns to this clip after its revive clip
TakeRoute:
  stx.b TMP
  rep #$20
  .ACCU 16
  txa
  asl a
  asl a
  asl a
  asl a
  clc
  adc.w #UnitTable
  tax
  sep #$20
  .ACCU 8
  lda.w U_FLAGS,x
  and.b #F_DEATHFILE
  beq +
  ldx.b UNIT
  stx.b RETURN
+
  ldx.b TMP
  ; (on into StartUnit)

; ---- start unit X: seek its stream, start its audio, from its first packet and decision
StartUnit:
  stx.b UNIT
  stz.b HOLDSCR
  rep #$20                      ; the ring: this unit and where the press list is
  .ACCU 16
  lda.b RINGI
  clc
  adc.w #4
  and.w #$003C
  sta.b RINGI
  tax
  lda.b UNIT
  sta.w RING,x
  lda.b SCRIPT
  sta.w RING+2,x
  sep #$20
  .ACCU 8
  jsr GradientOff               ; (a gradient scene turns it on again)
  ldx.w #$FFFF
  stx.b ROUTE                   ; no route chosen yet
  rep #$20
  .ACCU 16
  lda.b UNIT
  asl a
  asl a
  asl a
  asl a
  clc
  adc.w #UnitTable
  sta.b UPTR
  stz.b PK
  stz.b DEC
  lda.b UNIT
  lsr a
  lsr a
  lsr a
  tax                           ; X = its byte in VISIT
  lda.b UNIT
  and.w #$0007
  tay                           ; Y = its bit
  sep #$20
  .ACCU 8
  lda.b VISIT,x
  ora.w Bits,y
  sta.b VISIT,x                 ; visited
  ldx.b UPTR
  lda.w 0,x
  sta.w MSU_SEEK
  lda.w 1,x
  sta.w MSU_SEEK+1
  lda.w 2,x
  sta.w MSU_SEEK+2
  lda.w 3,x
  sta.w MSU_SEEK+3              ; seeks
  lda.w U_FLAGS,x
  and.b #F_TITLE|F_MENU|F_CHEAT
  beq +
  stz.w MSU_CONTROL             ; the start screen and the menus: no sound
-
  bit.w MSU_STATUS
  bmi -
  rts
+
  lda.w U_TRACK,x
  sta.w MSU_TRACK
  lda.w U_TRACK+1,x
  sta.w MSU_TRACK+1             ; loads the track (stops the one playing)
-
  bit.w MSU_STATUS              ; bit 7: data busy, bit 6: audio busy
  bmi -
  bvs -
  lda.b #$01
  sta.w MSU_CONTROL             ; play once
  rts

; ---- the start screen picture is on screen: directions move the outline to the nearest button that way, fire or
; start presses the button.  New Game: nothing visited, the first clip.  Continue: the saved clip and visited clips (a
; new game if there is no save).  The trailers and the intro just play.
TitleScreen:
  ldx.w #TitleButtons
  stx.b BTAB
  ldx.w #0
  stx.b TSEL
  stz.b OBANK
  stz.b CODE
  jsr TitleOutline
TitleHold:
  jsr Show
  jsr WaitBlank
  rep #$20
  .ACCU 16
  inc.b PK
  sep #$20
  .ACCU 8
  jsr Input
  lda.b NEW
  and.b #$3F
  beq TitleHold
  lda.b #1
  jsr Beep                      ; every action: the bad beep (as the 3DO)
  jsr CheatCode                 ; (does not come back if the code is complete)
  jsr ButtonInput
  bcs TitleHold
  ldx.w B_UNIT,y                ; fire or start: press it
  stx.b TMP
  lda.w B_ACTION,y
  cmp.b #2
  beq TitleGo
  cmp.b #1
  bne TitleNew
  jsr LoadGame
  bcc TitleNew                  ; no save
  stx.b TMP
  bra TitleFresh
TitleNew:
  ldx.w #63
-
  stz.b VISIT,x                 ; a new game: nothing visited
  dex
  bpl -
TitleFresh:
  ldx.w #$FFFF
  stx.b RETURN
TitleGo:
  ldx.b TMP
  jsr StartUnit
  jmp Packet

; the 3DO's cheat code on the start screen (launchme 0xE334): Up counts when the count is even, Down when it is odd,
; either resets it otherwise; Right resets it; Left with the count at 8 opens the cheat menu (the 3DO's test menu)
CheatCode:
  lda.b NEW
  and.b #$01                    ; up
  beq ++
  lda.b CODE
  and.b #$01
  bne +
  inc.b CODE
  bra ++
+
  stz.b CODE
++
  lda.b NEW
  and.b #$02                    ; down
  beq ++
  lda.b CODE
  and.b #$01
  beq +
  inc.b CODE
  bra ++
+
  stz.b CODE
++
  lda.b NEW
  and.b #$08                    ; right
  beq +
  stz.b CODE
+
  lda.b NEW
  and.b #$04                    ; left
  beq +
  lda.b CODE
  cmp.b #8
  bne +
  plx                           ; (it does not come back)
  stz.b CODE
  ldx.w #CHEAT_UNIT
  jsr StartUnit
  jmp Packet
+
  rts

; ---- the cheat menu picture is on screen (the 3DO's test menu): Right or Down the next entry, Left or Up the one
; before, fire or start plays its map (the map's presses go in as the joypad's, from its first clip, nothing visited),
; select goes back to the start screen (the 3DO's STOP button)
CheatScreen:
  ldx.w #CheatButtons
  stx.b BTAB
  ldx.w #0
  stx.b TSEL
  lda.b #$01
  sta.b OBANK
  jsr TitleOutline
CheatHold:
  jsr Show
  jsr WaitBlank
  rep #$20
  .ACCU 16
  inc.b PK
  sep #$20
  .ACCU 8
  jsr Input
  lda.b NEW
  and.b #$7F
  beq CheatHold
  lda.b #1
  jsr Beep                      ; every action: the bad beep
  lda.b NEW
  and.b #$40
  beq +
  ldx.w #SKIP_UNIT              ; select: the start screen
  bra CheatGo
+
  jsr ButtonInput
  bcs CheatHold
  ldx.w B_SCRIPT,y              ; fire or start: this map
  stx.b SCRIPT
  lda.b #1
  sta.b CHEATON
  ldx.w B_UNIT,y
  stx.b TMP
  ldx.w #63
-
  stz.b VISIT,x                 ; a new game: nothing visited
  dex
  bpl -
  ldx.w #$FFFF
  stx.b RETURN
  ldx.b TMP
CheatGo:
  jsr StartUnit
  jmp Packet

; a button pressed in a menu (NEW): a direction moves the outline, carry set; else carry clear.  Y = the selected
; button entry
ButtonInput:
  jsr TitleButton
  lda.b NEW
  ldx.w #0
-
  lsr a                         ; up, down, left, right
  bcs ButtonMove
  inx
  inx
  cpx.w #8
  bne -
  clc
  rts
ButtonMove:
  stx.b TMP
  rep #$20
  .ACCU 16
  tya
  clc
  adc.b TMP
  tax
  lda.w B_DIRS,x
  cmp.w #$FFFF
  sep #$20
  .ACCU 8
  beq +                         ; no button that way
  rep #$20
  .ACCU 16
  sta.b TSEL
  sep #$20
  .ACCU 8
  jsr TitleOutline
+
  sec
  rts

; Y = the selected button entry
TitleButton:
  rep #$20
  .ACCU 16
  lda.b TSEL
  asl a
  asl a
  asl a
  asl a
  clc
  adc.b BTAB
  tay
  sep #$20
  .ACCU 8
  rts

; the selected button outline: its OAM image by DMA (the screen is in forced blank here)
TitleOutline:
  jsr TitleButton
  ldx.w B_OAM,y
  stx.w $4302
  lda.b OBANK
  sta.w $4304
  stz.w $2102
  stz.w $2103                   ; OAM address 0
  stz.w $4300                   ; A to B, one register
  lda.b #$04
  sta.w $4301                   ; $2104
  ldx.w #544
  stx.w $4305
  lda.b #$01
  sta.w $420B
  rts

; ---- pause (3DO assets, the Sega CD rules).  START holds the picture with the PAUSE text and stops the sound; START
; again plays the clip from its start, fire opens the save menu.  Not in the clips any button skips, the start screen or
; the save menu, and only with a picture on screen: a press in a blank field (a scene cut) waits for the next picture.
PauseCheck:
  lda.b NEW
  and.b #$20
  ora.b WANTPAUSE
  sta.b WANTPAUSE
  beq PauseCheckDone
  ldx.b UPTR
  lda.w U_FLAGS,x
  and.b #F_SKIP|F_TITLE|F_MENU|F_CHEAT
  bne +
  lda.b FLAGS
  and.b #$01
  bne PauseCheckDone            ; a blank field
  stz.b WANTPAUSE
  plx                           ; (it does not come back here)
  jmp PauseScreen
+
  stz.b WANTPAUSE
PauseCheckDone:
  rts

; The stream keeps VRAM full, so the PAUSE text borrows BG1 tile slots that the picture on screen does not show.  That
; only works as a pause never goes back to the stream it stopped: the clip starts again, and any unit plays from junk.
; Three fields, the picture staying on screen: this blank reads the map, the picture time after it goes through the
; map, the next blank puts the text up.
PauseScreen:
  stz.w MSU_CONTROL             ; sound off
  jsr ResumeUnit
  stx.b RESUME
  jsr FindRewind
  lda.b CHEATON
  sta.b HOLDSCR                 ; (a cheat map: its presses wait while paused)
  lda.b #$80
  sta.w $2115                   ; BG1 map to work RAM
  ldx.w #$7C00
  stx.w $2116
  lda.b #$81                    ; B to A, mode 1 ($2139/$213A)
  sta.w $4300
  lda.b #$39
  sta.w $4301
  ldx.w #MAPBUF
  stx.w $4302
  stz.w $4304
  ldx.w #2048
  stx.w $4305
  lda.b #$01
  sta.w $420B
  jsr Show
  jsr PauseScan
  jsr WaitBlank
  jsr PausePlace
  ldx.w #0
  stx.b BLINK
PauseHold:
  jsr Show
  jsr WaitBlank
  rep #$20
  .ACCU 16
  inc.b PK
  inc.b BLINK
  sep #$20
  .ACCU 8
  jsr Input
  lda.b NEW
  and.b #$20
  bne PauseResume
  lda.b NEW
  and.b #$10
  bne PauseOpenMenu
  ldx.b BLINK                   ; the PAUSE text blinks: on / off every half second
  cpx.w #30
  bcc PauseHold
  ldx.w #0
  stx.b BLINK
  ldx.b PAGE
  cpx.w #$FFFF
  beq PauseHold                 ; (no text to blink)
  lda.b TM
  eor.b #$10
  sta.b TM
  sta.w $212C
  bra PauseHold
PauseOpenMenu:
  lda.b #$03
  sta.w $2101                   ; the sprite tiles of the menus
  ldx.w #MENU_UNIT
  jsr StartUnit
  jmp Packet
PauseResume:
  lda.b #$03
  sta.w $2101
  jsr Rewind
  ldx.b RESUME
  jsr StartUnit
  jmp Packet

; RSCRIPT = where the press list goes back to when the pause resumes: in a cheat map, where it was when RESUME last
; started (the clip plays again, so its presses must too); else (or not in the ring) where it is
FindRewind:
  rep #$20
  .ACCU 16
  lda.b SCRIPT
  sta.b RSCRIPT
  lda.b CHEATON
  and.w #$00FF
  beq ++
  lda.b RINGI
  sta.b TMP2
  ldy.w #16
-
  ldx.b TMP2
  lda.w RING,x
  cmp.b RESUME
  beq +
  txa
  sec
  sbc.w #4
  and.w #$003C
  sta.b TMP2
  dey
  bne -
  bra ++
+
  lda.w RING+2,x
  sta.b RSCRIPT
++
  sep #$20
  .ACCU 8
  rts

; a pause resumes: in a cheat map, the press list back to RSCRIPT
Rewind:
  lda.b CHEATON
  beq +
  ldx.b RSCRIPT
  stx.b SCRIPT
+
  rts

; X = the clip to play after a pause: this one, or for a death scene or a revive clip the clip it belongs to
ResumeUnit:
  ldx.b UPTR
  lda.w U_FLAGS,x
  and.b #F_REVIVE|F_DEATHFILE
  beq +
  ldx.b RETURN
  cpx.w #$FFFF
  bne ++
  ldx.w #START_UNIT
++
  rts
+
  ldy.w U_RESTART,x
  cpy.w #$FFFF
  bne +
  ldx.b UNIT
  rts
+
  tyx
  rts

; where the text goes.  The alternating buffers show one layer, so the tiles of the other are all free.  With both layers
; on: which BG1 tiles the map shows, and a page of them (256 tiles, one sprite name table) with room for the text.  The
; sprites are made here: PauseOam has the own tile numbers of the text.
PauseScan:
  lda.b TM
  and.b #$02
  bne +
  ldx.w #0                      ; BG2 is hidden: its first page, from word 0
  stx.b PAGE
  stx.b PADDR
  stz.b POBJ
  txy
-
  txa
  sta.w FREE,y
  inx
  iny
  cpy.w #PAUSE_TILES
  bne -
  jmp PageFound
+
  ldx.w #1023
-
  stz.w USED,x
  dex
  bpl -
  lda.b TM
  and.b #$01
  beq ScanDone                  ; BG1 is the hidden layer of the alternating buffers: none in use
  ldy.w #2046
-
  rep #$20
  .ACCU 16
  lda.w MAPBUF,y
  and.w #$03FF
  tax
  sep #$20
  .ACCU 8
  lda.b #1
  sta.w USED,x
  dey
  dey
  bpl -
ScanDone:
  ldx.w #0
  stx.b PAGE
PageLoop:
  ldy.w #0                      ; slots found
  lda.b PAGE
  xba
  lda.b #0                      ; A = the page x 256
  rep #$20
  .ACCU 16
  tax                           ; X = its first tile
  clc
  adc.w #256
  cmp.w #831                    ; (the last page: the ring ends here, BG2 map follows)
  bcc +
  lda.w #831
+
  sta.b TMP
  sep #$20
  .ACCU 8
-
  lda.w USED,x
  bne +
  txa
  sta.w FREE,y
  iny
  cpy.w #PAUSE_TILES
  beq PageMade
+
  inx
  cpx.b TMP
  bne -
  inc.b PAGE
  lda.b PAGE
  cmp.b #4
  bne PageLoop
  ldx.w #$FFFF
  stx.b PAGE                    ; no room (the picture stays, with no text)
  rts
PageMade:
  lda.b PAGE                    ; a page of BG1
  xba
  lda.b #0
  rep #$20
  .ACCU 16
  asl a
  asl a
  asl a
  asl a
  clc
  adc.w #$4000
  sta.b PADDR
  sep #$20
  .ACCU 8
  lda.b PAGE
  lsr a
  ora.b #$02                    ; sprite tiles from word $4000 or $6000; name table 1: $1000 words on
  sta.b POBJ
PageFound:
  ldx.w #543
-
  lda.w PauseOam,x
  sta.w OAMBUF,x
  dex
  bpl -
  ldx.w #0
-
  rep #$20
  .ACCU 16
  lda.w OAMBUF+2,x
  and.w #$00FF
  tay
  sep #$20
  .ACCU 8
  lda.w FREE,y
  sta.w OAMBUF+2,x
  lda.b PAGE
  and.b #$01                    ; the name table
  ora.w OAMBUF+3,x
  sta.w OAMBUF+3,x
  inx
  inx
  inx
  inx
  cpx.w #PAUSE_SPRITES*4
  bne -
  rts

; forced blank: the tiles, colours and sprites of the text, and the sprite layer on
PausePlace:
  ldx.b PAGE
  cpx.w #$FFFF
  bne +
  rts
+
  lda.b POBJ
  sta.w $2101
  lda.b #128
  sta.w $2121
  stz.w $4300                   ; A to B, one register
  lda.b #$22
  sta.w $4301
  ldx.w #PauseCgram
  stx.w $4302
  stz.w $4304
  ldx.w #6
  stx.w $4305
  lda.b #$01
  sta.w $420B
  lda.b #$80
  sta.w $2115
  lda.b #$01                    ; A to B, mode 1 ($2118/$2119)
  sta.w $4300
  lda.b #$18
  sta.w $4301
  ldy.w #0
  ldx.w #PauseTileData
  stx.b TMP2
  rep #$20
  .ACCU 16
PlaceTile:
  lda.w FREE,y
  and.w #$00FF
  asl a
  asl a
  asl a
  asl a
  clc
  adc.b PADDR
  sta.w $2116                   ; (and $2117)
  lda.b TMP2
  sta.w $4302
  clc
  adc.w #32
  sta.b TMP2
  lda.w #32
  sta.w $4305
  sep #$20
  .ACCU 8
  stz.w $4304
  lda.b #$01
  sta.w $420B
  rep #$20
  .ACCU 16
  iny
  cpy.w #PAUSE_TILES
  bne PlaceTile
  sep #$20
  .ACCU 8
  ldx.w #OAMBUF
  stx.w $4302
  stz.w $4304
  stz.w $2102
  stz.w $2103                   ; OAM address 0
  stz.w $4300
  lda.b #$04
  sta.w $4301                   ; $2104
  ldx.w #544
  stx.w $4305
  lda.b #$01
  sta.w $420B
  lda.b TM
  ora.b #$10
  sta.b TM
  sta.w $212C
  rts

; ---- the save menu picture is on screen: up and down move the highlight (Continue first), fire or start presses the
; button.  Continue: the clip again.  Save: keeps it, and the label turns green for a moment.  Exit: the start screen.
MenuScreen:
  ldx.w #MenuButtons
  stx.b BTAB
  stz.b OBANK
  ldx.w #MENU_FIRST
  stx.b TSEL
  ldx.w #0
  stx.b FLASH
  jsr TitleOutline
MenuHold:
  jsr Show
  jsr WaitBlank
  rep #$20
  .ACCU 16
  inc.b PK
  sep #$20
  .ACCU 8
  ldx.b FLASH
  beq +
  dex
  stx.b FLASH
  bne +
  ldx.w #LABEL_COLOUR
  jsr MenuColour
+
  jsr Input
  lda.b NEW
  and.b #$3F
  beq MenuHold
  jsr ButtonInput
  bcs MenuHold
  lda.w B_ACTION,y
  bne +
  jsr Rewind                    ; Continue
  ldx.b RESUME
  bra MenuGo
+
  cmp.b #1
  bne +
  jsr SaveGame
  ldx.w #30
  stx.b FLASH
  ldx.w #SAVED_COLOUR
  jsr MenuColour
  bra MenuHold
+
  lda.b CHEATON                 ; Exit: the start screen (a cheat map ends: its press list to its end)
  beq ExitGo
  stz.b CHEATON
  ldx.b SCRIPT
-
  rep #$20
  .ACCU 16
  lda.l $010000,x
  sep #$20
  .ACCU 8
  cmp.b #$FF
  bne +
  xba
  cmp.b #$FF
  beq ++
+
  inx
  inx
  inx
  inx
  inx
  bra -
++
  stx.b SCRIPT
ExitGo:
  ldx.w #SKIP_UNIT
MenuGo:
  jsr StartUnit
  jmp Packet

; X = the colour of the highlighted label (forced blank)
MenuColour:
  lda.b #129
  sta.w $2121
  rep #$20
  .ACCU 16
  txa
  sep #$20
  .ACCU 8
  sta.w $2122
  xba
  sta.w $2122
  rts

; ---- the saved game
SaveGame:
  ldx.w #3
-
  lda.w SaveMagic,x
  sta.l SAVE,x
  dex
  bpl -
  rep #$20
  .ACCU 16
  lda.w #BUILD_ID
  sta.l SAVE+4
  lda.b RESUME
  sta.l SAVE+6
  sep #$20
  .ACCU 8
  ldx.w #63
-
  lda.b VISIT,x
  sta.l SAVE+8,x
  dex
  bpl -
  rts

; carry set if there is a save of this build: VISIT filled, X = its clip
LoadGame:
  ldx.w #3
-
  lda.l SAVE,x
  cmp.w SaveMagic,x
  bne LoadGameNone
  dex
  bpl -
  rep #$20
  .ACCU 16
  lda.l SAVE+4
  cmp.w #BUILD_ID
  sep #$20
  .ACCU 8
  bne LoadGameNone
  ldx.w #63
-
  lda.l SAVE+8,x
  sta.b VISIT,x
  dex
  bpl -
  rep #$20
  .ACCU 16
  lda.l SAVE+6
  tax
  sep #$20
  .ACCU 8
  sec
  rts
LoadGameNone:
  clc
  rts

SaveMagic:
  .db "BD13"

; ---- NEW = the moves pressed in this field (from the joypad read in the last vertical blank, and the test script)
Input:
  lda.b #0
  xba                           ; clear A's high byte: TAX with 16-bit X copies it too
  lda.w $4219                   ; B Y select start up down left right
  and.b #$0F
  tax
  lda.w Dpad,x
  sta.b MOVES
  lda.w $4219
  and.b #$C0                    ; B, Y
  bne +
  lda.w $4218
  and.b #$C0                    ; A, X
  beq ++
+
  lda.b MOVES
  ora.b #$10                    ; fire
  sta.b MOVES
++
  lda.w $4219
  and.b #$10                    ; start
  beq +
  asl a
  ora.b MOVES
  sta.b MOVES
+
  lda.w $4219
  and.b #$20                    ; select: bit 6 (the cheat menu: back to the start screen, as the 3DO's STOP)
  beq +
  asl a
  ora.b MOVES
  sta.b MOVES
+
  lda.b PREV
  eor.b #$FF
  and.b MOVES
  sta.b NEW
  lda.b MOVES
  sta.b PREV
  lda.b HOLDSCR
  bne NoPress                   ; (paused in a cheat map)
  ldx.b SCRIPT                  ; a test or cheat press for this unit and field (in order, each once; ROM bank 1)
  rep #$20
  .ACCU 16
  lda.l $010000,x
  cmp.b UNIT
  bne NoPress
  lda.l $010002,x
  cmp.b PK
  bne NoPress
  sep #$20
  .ACCU 8
  lda.l $010004,x
  ora.b NEW
  sta.b NEW
  inx
  inx
  inx
  inx
  inx
  stx.b SCRIPT
  rts
NoPress:
  sep #$20
  .ACCU 8
  rts

Dpad:                           ; right, left, down, up (bits 0-3) -> up, down, left, right (bits 0-3)
  .db 0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15
Bits:
  .db 1, 2, 4, 8, 16, 32, 64, 128

; ---- this field's game logic: may start another unit
Decide:
  rep #$20
  .ACCU 16
  lda.b RAND                    ; 16-bit Galois shift register, a step per field
  lsr a
  bcc +
  eor.w #$B400
+
  sta.b RAND
  sep #$20
  .ACCU 8
  ldx.b UPTR
  lda.w U_FLAGS,x
  and.b #F_SKIP
  beq +
  lda.b NEW
  beq +
  ldx.w #SKIP_UNIT              ; the intro: any button
  jmp StartUnit
+
  ldx.b ROUTE
  cpx.w #$FFFF
  beq +
  ldx.b PK
  cpx.b CUT
  bcc +
  ldx.b ROUTE                   ; the route's switch field: go
  jmp TakeRoute
+
  ldx.b UPTR
  lda.w U_NDEC,x
  cmp.b DEC
  bne +
  jmp NoWindow                  ; no decisions left
+
  rep #$20
  .ACCU 16
  lda.b DEC
  asl a
  asl a
  sta.b TMP
  asl a
  asl a
  clc
  adc.b TMP                     ; x 20
  clc
  adc.w U_DEC,x
  tay                           ; Y = the decision
  lda.b PK
  cmp.w D_START,y
  bcs +
  sep #$20                      ; not open yet
  jmp NoWindow
+
  .ACCU 16
  cmp.w D_END,y
  sep #$20
  .ACCU 8
  lda.b #0                      ; window over: no move
  bcs Outcome
  lda.b NEW
  and.b #$1F
  bne +
  rts                           ; no move yet
+
  ldx.w #0
-
  inx                           ; the first move pressed: outcome 1-5
  lsr a
  bcc -
  txa
Outcome:
  rep #$20
  .ACCU 16
  and.w #$00FF
  asl a
  sta.b TMP2                    ; the outcome x 2 (0: no move)
  tya
  clc
  adc.b TMP2
  tax
  lda.w D_OUT,x
  sta.b TMP                     ; TMP = the outcome
  lda.w D_KIND,y
  bne RouteDecision
  inc.b DEC                     ; a death decision
  lda.b TMP
  cmp.w #$FFFF
  beq RightMove                 ; the right move: play on
  lda.b UNIT
  sta.b RETURN                  ; wrong: the death scene at once, the revive clip, then this clip again
  ldx.b TMP
  sep #$20
  .ACCU 8
  lda.b TMP2
  beq +                         ; (no move pressed: no sound)
  lda.b #1
  jsr Beep                      ; a wrong move: the bad beep
+
  jmp StartUnit
RightMove:
  .ACCU 16
  lda.b TMP2
  beq Undecided16               ; (no move needed: no sound)
  sep #$20
  .ACCU 8
  lda.b #0
  jmp Beep                      ; the right move: the good beep
RouteDecision:                  ; every new move in the window chooses again, the last one wins (as the 3DO)
  .ACCU 16
  lda.b TMP2
  bne +
  lda.b ROUTE                   ; window over: a way chosen in it stands
  cmp.w #$FFFF
  beq +
  inc.b DEC
  bra Undecided16
+
  jsr RouteOut                  ; lists and COND rules: TMP = the unit it leads to (keeps Y)
  lda.b TMP
  cmp.w #$FFFF
  bne +
  lda.b TMP2                    ; not offered: the bad beep, and a way chosen before is dropped
  bne RouteRefused
  inc.b DEC                     ; window over, nothing chosen: the clip plays on (a death for waiting)
  bra Undecided16
+
  sta.b ROUTE                   ; chosen: it takes over at the decision's switch field
  lda.w D_CUT,y
  sta.b CUT
  lda.b TMP2
  bne +
  inc.b DEC                     ; (a way taken with no move, at the window's end: no sound)
  bra Undecided16
+
  sep #$20
  .ACCU 8
  lda.b #0
  jmp Beep                      ; the good beep (the window stays open: a later move can change it)
NoWindow:                       ; no decision open: a move does nothing but the bad beep (as the 3DO)
  .ACCU 8
  lda.b NEW
  and.b #$1F
  beq +
  lda.b #1
  jmp Beep
+
  rts
RouteRefused:
  .ACCU 16
  lda.w #$FFFF
  sta.b ROUTE
  sep #$20
  .ACCU 8
  lda.b #1
  jmp Beep
Undecided16:
  sep #$20
  .ACCU 8
Undecided:
  rts

; ---- TMP = a route outcome of this unit -> the unit it leads to ($FFFF: not offered): a list gives one of its units
; at random; then the unit's COND rules, in order.  16-bit A on entry and exit; keeps Y.
RouteOut:
  .ACCU 16
  phy
  lda.b TMP
  cmp.w #$FFFF
  bne +
  jmp RouteOutDone
+
  cmp.w #$8000
  bcc Rules
  tax                           ; a list: count, units
  lda.b RAND
  and.w #$00FF
-
  cmp.w 0,x
  bcc +
  sec
  sbc.w 0,x
  bra -
+
  asl a
  sta.b LEFT                    ; (used as scratch: the rules below set it again)
  txa
  clc
  adc.b LEFT
  tax
  lda.w 2,x
  sta.b TMP
Rules:
  ldx.b UPTR
  lda.w U_CONDS,x
  bne +
  jmp RouteOutDone
+
  tax
  lda.w 0,x
  sta.b RULES
  inx
  inx
NextRule:
  lda.b RULES
  bne +
  jmp RouteOutDone
+
  dec.b RULES
  lda.w 0,x
  sta.b TARGET
  lda.w 2,x
  sta.b REPL
  lda.w 4,x
  sta.b LEFT
  stz.b MISSING
  inx
  inx
  inx
  inx
  inx
  inx
NextVisit:
  lda.b LEFT
  beq RuleDone
  dec.b LEFT
  lda.w 0,x                     ; a unit that must have been visited
  inx
  inx
  phx
  pha
  and.w #$0007
  tay
  pla
  lsr a
  lsr a
  lsr a
  tax
  sep #$20
  .ACCU 8
  lda.b VISIT,x
  and.w Bits,y
  rep #$20
  .ACCU 16
  plx
  and.w #$00FF
  bne NextVisit
  inc.b MISSING
  bra NextVisit
RuleDone:
  lda.b TMP
  cmp.b TARGET
  bne NextRule
  lda.b MISSING
  bne NextRule
  lda.b REPL                    ; the rule applies
  sta.b TMP
  bra NextRule
RouteOutDone:
  ply
  rts

; VLINE = the current scanline
GetLine:
  lda.w $2137                   ; latch the counters
  lda.w $213F                   ; reset the counter read toggles
  lda.w $213D
  sta.b VLINE
  lda.w $213D
  and.b #$01
  sta.b VLINE+1
  ldx.b VLINE
  rts

; wait for the start of the letterbox under the picture (first leave it, so one packet runs per field)
; then blank the screen there, in the horizontal blank before it (the H/V timer at dot 280 of the picture's last
; line, as Show does), so no part of the line under the picture (a background ring row) shows
WaitBlank:
-
  jsr GetLine
  cpx.w #LINE_BLANK-1
  bcs -
  ldx.w #LINE_BLANK-1
  stx.w $4209                   ; V timer
  ldx.w #280
  stx.w $4207                   ; H timer: in the horizontal blank
  lda.w $4211                   ; clear a pending timer flag
  lda.b #$31
  sta.w $4200                   ; H and V timer on, joypad auto-read on (no NMI)
  wai
  lda.b #$80
  sta.w $2100                   ; forced blank: VRAM and CGRAM can be written
  lda.b #$01
  sta.w $4200                   ; timer off
  lda.w $4211                   ; acknowledge the timer
  rts

; display on for the first picture line.  Ending forced blank in the middle of a line draws junk on it, so this
; waits for the horizontal blank before it: the H/V timer fires at dot 280 of the line above, and WAI wakes on it
; (interrupts stay masked, so there is no handler).  A packet that ran late turns the display on at once.
Show:
  jsr GetLine
  cpx.w #LINE_SHOW
  bcc +
  cpx.w #LINE_BLANK
  bcc On                        ; late: already in the picture
+
  ldx.w #LINE_SHOW-1
  stx.w $4209                   ; V timer
  ldx.w #280
  stx.w $4207                   ; H timer: in the horizontal blank
  lda.w $4211                   ; clear a pending timer flag
  lda.b #$31
  sta.w $4200                   ; H and V timer on, joypad auto-read on (no NMI)
  wai
On:
  lda.b #$0F
  sta.w $2100                   ; display on, full brightness
  lda.b #$01
  sta.w $4200                   ; timer off
  lda.w $4211                   ; acknowledge the timer
  rts

; ---- the button beeps (encode_beeps.py, spc_beeps.asm): A = 0 the good beep, 1 the bad one.  The sound chip plays
; it when port 0 changes; port 1 says which.  Keeps X and Y.
Beep:
  sta.w $2141
  inc.b BEEPN
  lda.b BEEPN
  sta.w $2140
  rts

; ---- boot: send the sound RAM image (SpcImage, ROM bank 2) to $0200 through the sound chip boot ROM, and start it
SpcUpload:
-
  ldx.w $2140
  cpx.w #$BBAA                  ; the boot ROM is ready
  bne -
  ldx.w #$0200
  stx.w $2142                   ; to $0200
  lda.b #$01
  sta.w $2141                   ; (non-zero: a block follows)
  lda.b #$CC
  sta.w $2140
-
  cmp.w $2140
  bne -
  ldx.w #0
SpcByte:
  lda.l $028000,x               ; (SpcImage)
  sta.w $2141
  txa                           ; the byte count, low byte
  sta.w $2140
-
  cmp.w $2140
  bne -
  inx
  cpx.w #SPC_SIZE
  bne SpcByte
  ldy.w #$0200
  sty.w $2142                   ; start the program at $0200
  stz.w $2141                   ; (zero: no more blocks)
  txa
  inc a                         ; the count + 1: not the next byte count, so the boot ROM ends the block
  sta.w $2140
  sta.b BEEPN
-
  cmp.w $2140
  bne -
  rts

.INCLUDE "units.inc"

.ENDS

; ROM bank 1 ($01:8000): the press lists (Input reads them with long addressing) and the cheat menu's OAM images
.BANK 1 SLOT 0
.ORG 0
.SECTION "Cheats" FORCE
.INCLUDE "cheats.inc"
.ENDS

; ROM bank 2 ($02:8000): the sound RAM image (encode_beeps.py)
.BANK 2 SLOT 0
.ORG 0
.SECTION "Sound" FORCE
.INCLUDE "spc.inc"
.ENDS

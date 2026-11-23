; SPC700 driver for the 3DO button beeps (encode_beeps.py assembles it with wla-spc700 and uploads it at $0200 with
; the sample directory at $0300 and the BRR samples from $0400; player.asm sends it there through the boot ROM).
; One voice, mono (the same volume both sides), no envelope: the CPU writes the sample number (0 good, 1 bad) to
; port 1, then a new value to port 0; the driver sees port 0 change and keys the voice on with that sample (a beep
; still playing starts again).  A sample ends on its BRR end block, which silences the voice.

.MEMORYMAP
  DEFAULTSLOT 0
  SLOTSIZE $0100
  SLOT 0 $0200
.ENDME
.ROMBANKSIZE $0100
.ROMBANKS 1
.BANK 0 SLOT 0
.ORG 0

.DEFINE LAST    $00             ; port 0 as last seen

Start:
  mov x,#0
DspLoop:
  mov a,!DspInit+x              ; DSP register, value ... $FF
  cmp a,#$FF
  beq DspDone
  mov $F2,a
  inc x
  mov a,!DspInit+x
  mov $F3,a
  inc x
  bra DspLoop
DspDone:
.IFDEF AUTOPLAY
  mov a,#0                      ; (encode_beeps.py test snapshot: the good beep, then the bad one)
  call !Play
  call !Wait
  mov a,#1
  call !Play
Idle:
  bra Idle
Wait:
  mov y,#0
-
  mov x,#0
--
  dec x
  bne --
  dec y
  bne -
  ret
.ENDIF
  mov a,$F4
  mov LAST,a
Loop:
  mov a,$F4                     ; port 0: a new value asks for a beep
  cmp a,LAST
  beq Loop
  mov LAST,a
  mov a,$F5                     ; port 1: which one
  call !Play
  bra Loop

; A = the sample: voice 0 plays it from the start
Play:
  mov $F2,#$04                  ; V0SRCN
  mov $F3,a
  mov $F2,#$4C                  ; KON
  mov $F3,#$01
  ret

DspInit:
  .db $6C, $20                  ; FLG: not reset, not muted, echo writes off
  .db $5C, $00                  ; KOF
  .db $4C, $00                  ; KON
  .db $0C, $7F, $1C, $7F        ; main volume, left and right
  .db $2C, $00, $3C, $00        ; echo volume off
  .db $4D, $00, $3D, $00, $2D, $00   ; no echo, noise or pitch modulation
  .db $6D, $FF, $7D, $00        ; echo buffer (unused) and delay 0
  .db $5D, $03                  ; the sample directory at $0300
  .db $00, $7F, $01, $7F        ; voice 0 volume: the same both sides (mono)
  .db $02, $06, $03, $0B        ; pitch $0B06: 22050 Hz samples (22050 / 32000 x $1000)
  .db $05, $00, $07, $7F        ; no ADSR: fixed gain, full
  .db $FF

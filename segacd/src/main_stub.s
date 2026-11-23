; MAIN.BIN - first thing the main CPU runs: the boot stub jumps here ($200000, Word RAM).
; Word RAM will be passed back and forth with the sub CPU, so copy the real player into Work RAM and run it there.

        org     $200000

stub:   lea     payload(pc),a0
        lea     $FF1000,a1
        move.w  #(payload_end-payload)/4-1,d0
.cp:    move.l  (a0)+,(a1)+
        dbra    d0,.cp
        jmp     $FF1000.l

        cnop    0,4
payload:
        incbin  "build/PAYLOAD.BIN"
        cnop    0,4
payload_end:

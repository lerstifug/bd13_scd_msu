# Brain Dead 13 for the Sega CD and the SNES (MSU-1)

Unofficial ports of ReadySoft's **Brain Dead 13** (1995) to the **Sega CD / Mega CD** and to the **SNES** with
the **MSU-1** CD-quality add-on, converted from the game's **3DO** version: its animation (backgrounds and cartoon
cels), its game logic, its menus and its button sounds. The SNES version can take its sound from the **CD-i**
version instead (the same mix, in stereo).

**This holds no game data.** The scripts convert your own disc images on your own computer. They check each disc
image against its [Redump](http://redump.org) entry first, so you know before a build of an hour or more that it
will work.

## The discs

| Disc | Redump | Size (bytes) | CRC32 | SHA-1 | Needed for |
|---|---|---|---|---|---|
| BrainDead 13 (USA) (Disc 1) (RE-1), 3DO, v1.1 | [76493](http://redump.org/disc/76493/) | 771413664 | 8d0d6228 | 995534c88c8df0a6ab16a7a1775d4bf061287c8d | both |
| BrainDead 13 (USA) (Disc 2), 3DO | [55470](http://redump.org/disc/55470/) | 506832480 | 747f1ca8 | 981dbcc139e7cc275c213cd027797fb9a15fcad4 | both |
| BrainDead 13 (Europe), CD-i, v1.08 | [67959](http://redump.org/disc/67959/) | 728964768 | 32748463 | 56042f99da19f9a79e1b406ab434ce6c90fb480b | SNES stereo sound (optional) |

Both 3DO discs are needed: the game's clips are split between them. The original v1.0 of disc 1
([55469](http://redump.org/disc/55469/), CRC32 939a863c) is accepted too, with a warning: it has the softlock bug
that v1.1 fixed, and these converters were made and tested with v1.1. Give the `.cue` files (or the `.bin` files,
or the folders holding them); `python discs.py <folder>` only checks them.

## What you get

- The whole game, all 402 clips and death scenes of both 3DO discs: Lance's moves in the decision windows, deaths
  and the revive scenes, the corridor choices with their random deaths, the item checks for the final part, the
  mirrored rooms, the ending.
- The intro on boot (any button skips it) and the 3DO start screen: New Game, Continue, the three ReadySoft
  trailers and the Brain Dead 13 logo (plays the intro again).
- Pause (START): the 3DO's blinking PAUSE text; fire on it opens the save menu (Save Game, Continue, Exit).
- The 3DO's cheat menu: on the start screen press Up Down Up Down Up Down Up Down, Left. It plays the game's own
  test runs by themselves (each area with or without its deaths); pause and Exit work while one plays.
- 12 frames per second like the 3DO version.

| | Sega CD | SNES MSU-1 |
|---|---|---|
| Picture | 224x192 in 256x224, 2 x 15 colours per 8x8 cell plus shadow | 252x216 (the 3DO's shape on the SNES's 8:7 pixels), 8 x 15 colours |
| Sound | 3DO, 14.4 kHz mono | 44.1 kHz: CD-i stereo, or 3DO mono |
| Saves | backup RAM | battery RAM (the .srm / .sav next to the ROM) |
| Size | one disc image, about 420 MB | 1.1 GB: ROM, MSU data, 402 audio tracks |
| Runs on | Sega CD / Mega CD, MiSTer Mega CD core, emulators | FXPak Pro / SD2SNES, MiSTer SNES core, bsnes, Snes9x 1.60+ |

Controls: the d-pad and the fire buttons (Sega CD A/B/C, SNES A/B/X/Y) are Lance's moves; START pauses. In the
intro and the trailers every button, START too, skips to the start screen. As on the 3DO, a move pressed when
nothing can be chosen plays the bad beep and does nothing else, so pressing early just beeps until the window
opens; in a route choice the last move pressed wins, and one the scene does not offer drops the choice.

The Dragon's Lair II trailer is drawn as one layer in a smaller picture (Sega CD 160x136, SNES 224x192): its many
scrolling backgrounds one after another would be too much data for either console at full size.

## Requirements

- Python 3.10 or newer with numpy and Pillow: `python -m pip install -r requirements.txt`
- Sega CD: [vasm](http://sun.hasenbraten.de/vasm/) (`vasmm68k_mot`) in `tools/vasm/`, and `US_BOOT.BIN` from
  Chilly Willy's Sega CD boot loader (cdboot, SpritesMind forum) in `tools/cdboot/`.
- SNES: [WLA-DX](https://github.com/vhelin/wla-dx) (`wla-65816`, `wla-spc700`, `wlalink`) in `tools/wla-dx/` or on
  the PATH; for the CD-i sound, [ffmpeg](https://ffmpeg.org/download.html) in `tools/ffmpeg/` or on the PATH.

No tool is included (their licences only allow their own archives to be passed on).

## Building

```
python build_segacd.py "D:\discs\BrainDead 13"
python build_snes.py "D:\discs\BrainDead 13"
```

(a folder holding the disc images, or the `.cue` files one by one). Results:

- `output/segacd/bd13.cue` + `bd13.bin`: load the `.cue` in an emulator or on a MiSTer, or burn it.
- `output/snes/BrainDead13-MSU1.zip`: `bd13msu.sfc`, `bd13msu.msu` and `bd13msu-1.pcm` ... `bd13msu-402.pcm`
  in a folder `bd13msu/`. Unzip it to your SD card; the files must stay together with these names. `--folder`
  writes the folder instead of a zip.

Both builds share their first step (the 3DO export), so the second one is quicker. Times on a recent PC:

| Step | Sega CD | SNES |
|---|---|---|
| 3DO export (shared) | 5 min | 5 min |
| conversion | 1.5 h (bitmaps, 3 workers) + 20 min (streams) | 3 h (every clip, 4 workers) |
| disc / packing | 1 min | 2 min |
| disk space while building | 25 GB | 12 GB |

Each worker can use 2-3 GB of RAM; use `--workers` to match your memory. A build that stops (or is stopped) goes
on from the step it was in when the same command is run again (`build_state.json`); `--redo STEP` does a step
again.

## How it works

Every clip of the 3DO game is a stream of frames: a background (sometimes a panorama the camera moves over),
cartoon cels drawn over it, and the sound. `bd13/3do/export_3do.py` writes them as plain files (PNG, WAV, JSON:
`bd13/3do/extracted/`), with the game logic (each decision's window and outcomes, the random deaths, the item
checks) decoded from the 3DO program's own tables.

**Sega CD** (`segacd/`): `scd_bitmaps.py` makes every colour decision and writes each unit as editable 8-bit
BMPs (`segacd/scd/`; magenta is transparent, `scd_format.py` has the rules), so the picture can be retouched
in Aseprite or similar and rebuilt (`build_segacd.py --redo pack`). `scd_pack.py` packs them into the player's
streams and checks each decoded frame against its bitmap. The sub 68000 (`src/app.s`) streams each clip at the
drive's full speed into a ring, reads the next clip ahead, builds each frame's tiles and maps in Word RAM, plays
the PCM audio (the picture follows the audio clock), and runs the game logic and the menus; the main 68000
(`src/payload.s`) DMAs the frames to the video chip.

**SNES** (`snes_msu1/poc_src/`): `encode_snes.py` turns each unit into a stream of per-field DMA packets: the
background on BG2 (a tile ring both ways for the scrolling scenes, at the 3DO's 12 frames per second), the cels on
BG1 with up to 8 palettes per frame, scenes too busy for that drawn whole in alternating buffers, and plain
gradient backgrounds as HDMA colour gradients. Every unit is checked by a simulation of the SNES's picture chip
before it is packed. The player (`player.asm`) streams the MSU-1 data, plays the audio track of each unit, and
runs the same game logic and menus as the Sega CD; the button beeps are the 3DO's, played by the sound chip
(`spc_beeps.asm`). `check_cheat.py` runs the finished ROM in a 65816 interpreter and plays all 27 of the game's
test runs through the cheat menu.

## Layout

```
build_segacd.py, build_snes.py   the builds          discs.py   the Redump check      steps.py   shared
bd13/3do/      3DO disc reader, game data export      bd13/cdi/  CD-i MPEG track extraction
segacd/        Sega CD converter and player           snes_msu1/poc_src/  SNES converter and player
tools/         put the tools here (see each README)   output/    the results
```

Brain Dead 13 is (c) ReadySoft / Epicenter Interactive. This is a fan project, not affiliated with them, and holds
none of their data.

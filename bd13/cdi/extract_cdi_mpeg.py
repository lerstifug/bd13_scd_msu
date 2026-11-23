#!/usr/bin/env python3
"""
Extract all MPEG-1 tracks from the Philips CD-i BrainDead 13 disc image
('BrainDead 13 (Europe).bin') into standalone, compliant MPEG-1 System (.mpg) files.

Why raw extraction from bd13.rtf fails in standard players/indexers:
1. Each of the 482 tracks in 'bd13.rtf' is preceded by a 2048-byte Mode 2 Form 1
   metadata sector (subheader 01004800, magic 0xCAFEBABE) containing segment and
   branching metadata, followed by 2324-byte Mode 2 Form 2 MPEG-1 Audio/Video sectors
   up to an End-Of-Record (EOR) sector.
2. In the CD-i MPEG-1 packs, the initial System Clock Reference (SCR) and first I-frame
   Decoding Timestamp (DTS) are initialized to negative values (e.g., SCR = -25346,
   DTS = -3750 ticks in 33-bit two's complement, i.e., ~8589930842 unsigned) so that the
   first Presentation Timestamp (PTS) starts at 0. Standard demuxers interpret this as
   a 26.5-hour timestamp wrap-around between frame 0 and frame 1.
3. Audio sectors (coding 0x7F) end their PES packet at byte 2304 with 20 trailing zero
   bytes (2304..2324) outside any PES packet.
4. Sector LBA 144724 (relative sector 139360 in bd13.rtf) has a minor C2 burst error in
   its first subheader copy and MPEG pack/PES header bytes (which also caused the
   pre-extracted bd13.rtf file to include 12 extra header/trailer bytes at that sector).
5. In addition to the 482 tracks in 'bd13.rtf', 'misc.rtf' contains 1 MPEG-1 track
   ('BUMPER' at LBA 2873..4088).
"""

import csv
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# the disc image: the first argument (its .bin), else the Redump name next to this script; the tracks go next to it
BIN_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "BrainDead 13 (Europe).bin")
OUT_DIR = os.path.join(HERE, "mpeg_tracks")
SECTOR_SIZE = 2352

# Shift all 33-bit 90kHz timestamps by +45000 ticks (+0.500s) so all SCR/PTS/DTS >= 0
TS_OFFSET = 45000
MASK_33 = (1 << 33) - 1


def shift_ts(b: bytes, offset: int = TS_OFFSET) -> bytes:
    """Shift a 5-byte MPEG-1 33-bit timestamp (SCR, PTS, or DTS) by `offset` ticks."""
    prefix = b[0] & 0xF0
    v = (
        ((b[0] & 0x0E) << 29)
        | (b[1] << 22)
        | ((b[2] & 0xFE) << 14)
        | (b[3] << 7)
        | (b[4] >> 1)
    )
    sv = v - (1 << 33) if v >= (1 << 32) else v
    nv = (sv + offset) & MASK_33
    b0 = prefix | ((nv >> 29) & 0x0E) | 0x01
    b1 = (nv >> 22) & 0xFF
    b2 = ((nv >> 14) & 0xFE) | 0x01
    b3 = (nv >> 7) & 0xFF
    b4 = ((nv << 1) & 0xFE) | 0x01
    return bytes([b0, b1, b2, b3, b4])


def process_mpeg_pack(payload: bytes, lba: int) -> bytearray:
    """Fix timestamps, repair LBA 144724 header, and strip trailing sector zero-padding."""
    p = bytearray(payload)
    if lba == 144724:
        # Reconstruct deterministic pack + video PES header corrupted by CD read error
        p[0:19] = bytes.fromhex("000001ba2100032add801b91000001e00902ff")

    # Shift Pack SCR (bytes 4..9)
    p[4:9] = shift_ts(p[4:9])

    pos = 12
    plen_total = len(p)
    while pos + 6 <= plen_total and p[pos : pos + 3] == b"\x00\x00\x01":
        sid = p[pos + 3]
        if sid == 0xB9:  # MPEG_program_end_code
            pos += 4
            break
        pkt_len = (p[pos + 4] << 8) | p[pos + 5]
        if sid in (0xC0, 0xE0):
            q = pos + 6
            while q < plen_total and p[q] == 0xFF:
                q += 1
            if q < plen_total and (p[q] & 0xC0) == 0x40:
                q += 2
            if q < plen_total:
                flags = p[q] & 0xF0
                if flags == 0x20:
                    p[q : q + 5] = shift_ts(p[q : q + 5])
                elif flags == 0x30:
                    p[q : q + 5] = shift_ts(p[q : q + 5])
                    p[q + 5 : q + 10] = shift_ts(p[q + 5 : q + 10])
        pos += 6 + pkt_len

    return p[:pos]


def read_rtls_index(f) -> dict:
    """Read the 482-entry 'rtls' segment table from misc.rtf (LBA 2864..2870)."""
    f.seek(2864 * SECTOR_SIZE)
    rtls_data = bytearray()
    for _ in range(7):
        raw = f.read(SECTOR_SIZE)
        rtls_data.extend(raw[24 : 24 + 2048])

    count = struct.unpack(">I", rtls_data[0x25DC:0x25E0])[0]
    rel_to_name = {}
    for i in range(count):
        off = 0x25E0 + i * 8
        str_off, rel_sec = struct.unpack(">II", rtls_data[off : off + 8])
        s_end = rtls_data.find(b"\x00", str_off)
        name = rtls_data[str_off:s_end].decode("ascii")
        rel_to_name[rel_sec] = (i, name)
    return rel_to_name


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest_rows = []

    with open(BIN_FILE, "rb") as f:
        rel_to_name = read_rtls_index(f)
        print(f"Loaded {len(rel_to_name)} segment names from misc.rtf rtls table.")

        # 1. Extract BUMPER from misc.rtf (LBA 2873..4088)
        bumper_out = bytearray()
        vid_secs = 0
        aud_secs = 0
        for lba in range(2873, 4089):
            f.seek(lba * SECTOR_SIZE)
            raw = f.read(SECTOR_SIZE)
            sub = raw[20:24]
            if sub[3] in (0x0F, 0x7F):
                if sub[3] == 0x0F:
                    vid_secs += 1
                else:
                    aud_secs += 1
                bumper_out.extend(process_mpeg_pack(raw[24 : 24 + 2324], lba))
        bumper_out.extend(b"\x00\x00\x01\xb9")
        bumper_fname = "BUMPER.mpg"
        bumper_path = os.path.join(OUT_DIR, bumper_fname)
        with open(bumper_path, "wb") as out_f:
            out_f.write(bumper_out)
        manifest_rows.append({
            "disc_order": 0,
            "rtls_index": -1,
            "name": "BUMPER",
            "filename": bumper_fname,
            "source_file": "misc.rtf",
            "start_lba": 2873,
            "end_lba": 4088,
            "video_sectors": vid_secs,
            "audio_sectors": aud_secs,
            "size_bytes": len(bumper_out),
        })
        print(f"Extracted {bumper_fname} ({len(bumper_out):,} bytes)")

        # 2. Extract all 482 tracks from bd13.rtf (LBA 5364..309567)
        sorted_rels = sorted(rel_to_name.keys())
        total_bd_sectors = 304204
        BD_START_LBA = 5364

        for disc_idx, start_rel in enumerate(sorted_rels, start=1):
            end_rel = (
                sorted_rels[disc_idx] - 1
                if disc_idx < len(sorted_rels)
                else total_bd_sectors - 1
            )
            rtls_idx, seg_name = rel_to_name[start_rel]
            start_lba = BD_START_LBA + start_rel
            end_lba = BD_START_LBA + end_rel

            f.seek(start_lba * SECTOR_SIZE)
            track_out = bytearray()
            vid_secs = 0
            aud_secs = 0

            for lba in range(start_lba, end_lba + 1):
                raw = f.read(SECTOR_SIZE)
                sub = raw[20:24]
                if (sub[2] & 0x20) == 0:
                    # Mode 2 Form 1 cafebabe segment header sector
                    continue
                if sub[3] == 0x0F:
                    vid_secs += 1
                elif sub[3] == 0x7F:
                    aud_secs += 1
                track_out.extend(process_mpeg_pack(raw[24 : 24 + 2324], lba))

            track_out.extend(b"\x00\x00\x01\xb9")
            fname = f"{seg_name}.mpg"
            fpath = os.path.join(OUT_DIR, fname)
            with open(fpath, "wb") as out_f:
                out_f.write(track_out)

            manifest_rows.append({
                "disc_order": disc_idx,
                "rtls_index": rtls_idx,
                "name": seg_name,
                "filename": fname,
                "source_file": "bd13.rtf",
                "start_lba": start_lba,
                "end_lba": end_lba,
                "video_sectors": vid_secs,
                "audio_sectors": aud_secs,
                "size_bytes": len(track_out),
            })

            if disc_idx % 50 == 0 or disc_idx == len(sorted_rels):
                print(f"  Processed {disc_idx}/{len(sorted_rels)} tracks from bd13.rtf...")

    manifest_path = os.path.join(OUT_DIR, "tracks_manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=list(manifest_rows[0].keys()))
        writer.writeheader()
        writer.writerows(manifest_rows)

    elapsed = time.time() - t0
    print(f"Done! Extracted {len(manifest_rows)} MPEG tracks to '{OUT_DIR}/' in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()

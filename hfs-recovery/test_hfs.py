#!/usr/bin/env python3
"""End-to-end test: build synthetic HFS+ image, parse, walk, extract."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_test_image import build_image, FILE_CONTENT
from build_test_image import (build_overflow_image, build_hardlink_image,
                              build_compressed_image, HLINK_CONTENT)
from build_test_image import (build_crosslink_image, build_journal_image,
                              build_alternate_header_image,
                              JOURNAL_INFO_BLOCK_NUM, JOURNAL_SIZE)
from hfs_parse import HFSVolume, parse_volume_header, VOLUME_HEADER_OFFSET
from hfs_parse import (is_compressed, resolve_hardlink, CompressedFileError,
                       read_fork_full, FORK_DATA)
from hfs_parse import (detect_crosslinks, check_journal,
                       try_alternate_header)
from scan_nodes import scan

IMG = "/tmp/test-hfs.img"


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        sys.exit(1)


def main():
    img = build_image()
    with open(IMG, "wb") as f:
        f.write(img)

    # 1. Volume header
    hdr = parse_volume_header(img, VOLUME_HEADER_OFFSET)
    check("signature is H+", hdr["signature"] == 0x482B)
    check("block_size 4096", hdr["block_size"] == 4096)
    check("catalog extent -> blocks 2-3",
          hdr["catalog_file"]["extents"] == [
              {"start_block": 2, "block_count": 2}])
    check("file_count==1 folder_count==2",
          hdr["file_count"] == 1 and hdr["folder_count"] == 2)

    # 2. B-tree walk
    vol = HFSVolume(IMG)
    check("node_size 4096", vol.node_size == 4096)
    check("btree_type catalog", vol.btree["btree_type"] == 0)
    records = list(vol.iter_catalog_records())
    check("6 catalog records harvested", len(records) == 6)

    types = sorted(r["type_name"] for _k, r in records)
    check("record types as expected",
          types == ["file", "file_thread", "folder", "folder",
                    "folder_thread", "folder_thread"])

    # 3. Path resolution
    threads = vol.build_thread_index()
    check("thread index has CNIDs 2,16,17",
          set(threads) == {2, 16, 17})
    path = vol.resolve_path(17, threads)
    check("resolve /Docs/hello.txt", path == "/Docs/hello.txt")
    print("      resolved:", path)

    # 4. File extraction via extents
    for key, rec in records:
        if rec["type_name"] == "file":
            data = vol.read_fork(rec["data_fork"])
            check("extracted bytes match", data == FILE_CONTENT)
            print("      extracted %d bytes" % len(data))
    vol.close()

    # 5. Node-signature scan finds both B-tree nodes
    hits = [(off, desc) for off, desc, rc in scan(img, 4096)
            if rc is not None]
    offs = sorted(o for o, _d in hits)
    cat_base = 1024 + 2 * 4096
    check("scanner finds header+leaf nodes",
          offs == [cat_base, cat_base + 4096])
    print("      node offsets:", offs)

    # 6. CLI smoke test
    out = subprocess.run(
        [sys.executable, "hfs_parse.py", IMG, "--list"],
        capture_output=True, text=True, cwd=os.path.dirname(IMG) or ".")
    # run from project dir instead
    out = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "hfs_parse.py"), IMG, "--list"],
        capture_output=True, text=True)
    check("CLI --list shows /Docs/hello.txt", "/Docs/hello.txt" in out.stdout)
    print("      CLI output:\n" + "\n".join(
        "        " + l for l in out.stdout.strip().splitlines()[:8]))

    print("\nALL TESTS PASSED")


def test_g2_extents_overflow():
    """G2: file with >8 extents chains through the extents-overflow B-tree."""
    img, expected, fid = build_overflow_image()
    with open("/tmp/test-hfs-overflow.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-overflow.img")

    # Overflow B-tree is discoverable
    orecs = list(vol.iter_extents_overflow_records())
    check("one overflow record found", len(orecs) == 1)
    key, exts = orecs[0]
    check("overflow key matches (data, fid, startBlock=8)",
          key["fork_type"] == FORK_DATA and key["file_id"] == fid
          and key["start_block"] == 8)
    check("overflow has 4 extents", len(exts) == 4)

    # Full extraction chains inline + overflow
    rec = None
    for _k, r in vol.iter_catalog_records():
        if r.get("type_name") == "file" and r.get("file_id") == fid:
            rec = r
    check("big.bin record found", rec is not None)
    check("inline extents are 8 (not enough alone)",
          len(rec["data_fork"]["extents"]) == 8)
    data = vol.extract_file(rec)
    check("extract_file returns full 48KB via overflow", data == expected)
    print("      chained %d bytes" % len(data))
    vol.close()


def test_g4_hardlink():
    """G4: hard link resolves to iNode file's data."""
    img = build_hardlink_image()
    with open("/tmp/test-hfs-hardlink.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-hardlink.img")

    link_rec = None
    for _k, r in vol.iter_catalog_records():
        if r.get("type_name") == "file" and r.get("file_id") == 21:
            link_rec = r
    check("mylink.txt record found", link_rec is not None)
    check("link record not flagged compressed",
          not is_compressed(link_rec))

    target = resolve_hardlink(vol, link_rec)
    check("hard link resolves to a record", target is not None)
    check("target is iNode42", target.get("file_id") == 20)

    data = vol.extract_file(link_rec)
    check("extract_file follows hard link", data == HLINK_CONTENT)
    print("      resolved %d bytes via iNode42" % len(data))

    # A normal file is not treated as a hard link
    plain = {"flags": 0, "finder_info": b"\x00" * 32,
             "bsd_info": {"special": 0}}
    check("non-link returns None", resolve_hardlink(vol, plain) is None)
    vol.close()


def test_g3_compressed():
    """G3: decmpfs-suspect files are flagged, never silently extracted."""
    img = build_compressed_image()
    with open("/tmp/test-hfs-compressed.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-compressed.img")

    rec = None
    for _k, r in vol.iter_catalog_records():
        if r.get("type_name") == "file":
            rec = r
    check("comp.dat record found", rec is not None)
    check("is_compressed flags xattr bit", is_compressed(rec))

    try:
        vol.extract_file(rec)
        check("extract_file refuses compressed file", False)
    except CompressedFileError:
        check("extract_file refuses compressed file", True)

    # A plain file is not flagged
    plain = {"flags": 0}
    check("plain file not flagged", not is_compressed(plain))
    vol.close()


def test_g5_crosslinks():
    """G5: two files claiming the same block are flagged; clean is []."""
    img = build_crosslink_image(crosslinked=True)
    with open("/tmp/test-hfs-crosslink.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-crosslink.img")
    links = detect_crosslinks(vol)
    check("one cross-linked range found", len(links) == 1)
    (start, end), paths = links[0]
    check("shared range is block 4 only", (start, end) == (4, 5))
    check("both claimant paths reported",
          paths == ["/Docs/a.txt", "/Docs/b.txt"])
    print("      cross-link blocks %d..%d: %s" % (start, end - 1, paths))
    vol.close()

    # Clean image: distinct blocks -> no cross-links
    img = build_crosslink_image(crosslinked=False)
    with open("/tmp/test-hfs-clean.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-clean.img")
    check("clean image has no cross-links", detect_crosslinks(vol) == [])
    vol.close()

    # Base image (one file) and hard-link image are also clean:
    # the hard link shares its iNode's blocks legitimately (G4).
    img = build_image()
    vol = HFSVolume(img)
    check("base image has no cross-links", detect_crosslinks(vol) == [])
    vol.close()
    vol = HFSVolume(build_hardlink_image())
    check("hard-link sharing is not a cross-link",
          detect_crosslinks(vol) == [])
    vol.close()


def test_g6_journal():
    """G6: journal assessment (read-only) on synthetic journals."""
    # Non-empty journal, clean unmount
    img, joff = build_journal_image(non_empty=True, clean=True)
    with open("/tmp/test-hfs-journal.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-journal.img")
    rep = check_journal(img, vol)
    check("journal present", rep["journal_present"])
    check("journal info block is 10",
          rep["journal_info_block"] == JOURNAL_INFO_BLOCK_NUM)
    check("journal offset points at block 11",
          rep["journal_offset"] == joff)
    check("journal size is 8192", rep["journal_size"] == JOURNAL_SIZE)
    check("journal header valid", rep["journal_header_valid"])
    check("non-empty journal detected", rep["non_empty"])
    check("non-empty journal needs replay", rep["needs_replay"])
    check("pending bytes counted", rep["pending_bytes"] == 4096)
    check("clean unmount reported", not rep["unclean_unmount"])
    check("no error", rep["error"] is None)
    print("      journal @%d size %d pending %d"
          % (joff, rep["journal_size"], rep["pending_bytes"]))

    # Same assessment via a header dict instead of a volume object
    rep2 = check_journal(img, vol.header)
    check("check_journal accepts a header dict",
          rep2["journal_present"] and rep2["non_empty"])
    vol.close()

    # Empty journal: start == end -> nothing pending
    img, _joff = build_journal_image(non_empty=False, clean=True)
    vol = HFSVolume(img)
    rep = check_journal(img, vol)
    check("empty journal present", rep["journal_present"])
    check("empty journal is not non-empty", not rep["non_empty"])
    check("empty journal needs no replay", not rep["needs_replay"])
    check("empty journal pending bytes 0", rep["pending_bytes"] == 0)
    vol.close()

    # Unclean unmount: journaled, clean bit clear
    img, _joff = build_journal_image(non_empty=True, clean=False)
    vol = HFSVolume(img)
    rep = check_journal(img, vol)
    check("unclean unmount reported", rep["unclean_unmount"])
    check("unclean + non-empty still needs replay", rep["needs_replay"])
    vol.close()

    # No journal at all (base image)
    img = build_image()
    vol = HFSVolume(img)
    rep = check_journal(img, vol)
    check("unjournaled volume has no journal",
          not rep["journal_present"] and not rep["non_empty"]
          and not rep["needs_replay"])
    vol.close()

    # Corrupt journal header magic -> invalid, not trusted
    img, joff = build_journal_image(non_empty=True, clean=True)
    bad = bytearray(img)
    bad[joff: joff + 4] = b"\x00\x00\x00\x00"
    vol = HFSVolume(bytes(bad))
    rep = check_journal(bytes(bad), vol)
    check("bad journal magic is invalid", not rep["journal_header_valid"])
    check("bad journal magic is not non-empty", not rep["non_empty"])
    check("bad journal magic reports an error", rep["error"] is not None)
    vol.close()


def test_g7_alternate_header():
    """G7: corrupt primary + valid alternate parses; both bad raises."""
    # Direct helper: alternate parses on the fixture image
    img = build_alternate_header_image(corrupt_primary=True)
    alt = try_alternate_header(img, len(img))
    check("alternate header signature is H+", alt["signature"] == 0x482B)
    check("alternate header block_size 4096", alt["block_size"] == 4096)

    # HFSVolume falls back to the alternate and still reads the catalog
    with open("/tmp/test-hfs-alt.img", "wb") as f:
        f.write(img)
    vol = HFSVolume("/tmp/test-hfs-alt.img")
    check("volume fell back to alternate header",
          vol.header_source == "alternate")
    check("catalog still resolves via alternate header",
          vol.resolve_path(17) == "/Docs/hello.txt")
    vol.close()

    # Bytes source works too (no file involved)
    vol = HFSVolume(img)
    check("bytes source also falls back", vol.header_source == "alternate")
    vol.close()

    # Primary intact -> primary is used, not the alternate
    img_ok = build_alternate_header_image(corrupt_primary=False)
    vol = HFSVolume(img_ok)
    check("intact primary is used", vol.header_source == "primary")
    vol.close()

    # Both corrupt -> ValueError (primary error + alternate error)
    bad = bytearray(img)
    bad[len(bad) - 1024: len(bad) - 1024 + 2] = b"\x00\x00"
    try:
        HFSVolume(bytes(bad))
        check("both headers corrupt raises ValueError", False)
    except ValueError:
        check("both headers corrupt raises ValueError", True)

    # No alternate at all (base image) -> helper raises ValueError
    try:
        try_alternate_header(build_image(), len(build_image()))
        check("missing alternate raises ValueError", False)
    except ValueError:
        check("missing alternate raises ValueError", True)


if __name__ == "__main__":
    main()
    print("\n--- G2 extents overflow ---")
    test_g2_extents_overflow()
    print("\n--- G4 hard links ---")
    test_g4_hardlink()
    print("\n--- G3 compression guard ---")
    test_g3_compressed()
    print("\n--- G5 cross-linked extents ---")
    test_g5_crosslinks()
    print("\n--- G6 journal assessment ---")
    test_g6_journal()
    print("\n--- G7 alternate volume header ---")
    test_g7_alternate_header()
    print("\nALL GAP TESTS PASSED")

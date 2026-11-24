#!/usr/bin/env python3
"""
build_test_image.py -- construct a minimal synthetic HFS+ volume.

Layout (block_size=4096, 64 blocks):
  byte 0..1023      boot blocks (zero)
  byte 1024..1535   volume header
  blocks 2..3       catalog B-tree (header node + 1 leaf node)
  block 4           data of /Docs/hello.txt

Catalog leaf holds: root folder (CNID 2), Docs folder (CNID 16),
hello.txt file (CNID 17), plus the three thread records.

Usage: python3 build_test_image.py [output_path]
"""

import struct
import sys

BLOCK_SIZE = 4096
TOTAL_BLOCKS = 64
FILE_CONTENT = b"Hello, HFS+! This file was extracted via raw extents.\n"


def u16(v):
    return struct.pack(">H", v)


def u32(v):
    return struct.pack(">I", v)


def u64(v):
    return struct.pack(">Q", v)


def catalog_key(parent_id, name):
    name_b = name.encode("utf-16-be")
    key_body = u32(parent_id) + u16(len(name)) + name_b
    return u16(len(key_body)) + key_body


def folder_record(folder_id, valence=0):
    bsd = u32(501) + u32(20) + b"\x00\x00" + u16(0o40755) + u32(0)
    finder_info = b"\x00" * 32  # DInfo (16) + DXInfo (16)
    return (u16(0x0001) + u16(0) + u32(valence) + u32(folder_id)
            + u32(0) * 5 + bsd + finder_info + u32(0) + u32(0))


def file_record(file_id, data_start_block, data_len):
    bsd = u32(501) + u32(20) + b"\x00\x00" + u16(0o100644) + u32(0)
    finder_info = b"\x00" * 32  # FInfo (16) + FXInfo (16)
    data_fork = (u64(data_len) + u32(4096) + u32(1)
                 + u32(data_start_block) + u32(1) + b"\x00" * 56)
    rsrc_fork = u64(0) + u32(4096) + u32(0) + b"\x00" * 64
    return (u16(0x0002) + u16(0) + u32(0) + u32(file_id)
            + u32(0) * 5 + bsd + finder_info + u32(0) + u32(0)
            + data_fork + rsrc_fork)


def file_record_extents(file_id, extents, logical_size, flags=0,
                         ftype=b"\x00" * 4, fcreator=b"\x00" * 4,
                         special=0):
    """File record with arbitrary inline extents and Finder/flag control.

    extents: list of (start_block, block_count), up to 8 inline.
    """
    bsd = (u32(501) + u32(20) + b"\x00\x00" + u16(0o100644)
           + u32(special))
    finder_info = ftype + fcreator + b"\x00" * 24  # FInfo + FXInfo
    ext_bytes = b"".join(u32(s) + u32(c) for s, c in extents)
    ext_bytes += b"\x00" * (64 - len(ext_bytes))
    total = sum(c for _s, c in extents)
    data_fork = u64(logical_size) + u32(4096) + u32(total) + ext_bytes
    rsrc_fork = u64(0) + u32(4096) + u32(0) + b"\x00" * 64
    return (u16(0x0002) + u16(flags) + u32(0) + u32(file_id)
            + u32(0) * 5 + bsd + finder_info + u32(0) + u32(0)
            + data_fork + rsrc_fork)


def extent_key(fork_type, file_id, start_block):
    """HFSPlusExtentKey bytes with 2-byte length prefix."""
    body = struct.pack(">BBII", fork_type, 0, file_id, start_block)
    return u16(len(body)) + body


def extent_record(extents):
    """64-byte extent record: 8 (startBlock, blockCount) pairs."""
    rec = b"".join(u32(s) + u32(c) for s, c in extents)
    return rec + b"\x00" * (64 - len(rec))


def thread_record(rtype, parent_id, name):
    name_b = name.encode("utf-16-be")
    return u16(rtype) + u16(0) + u32(parent_id) + u16(len(name)) + name_b


def build_leaf_node(records, node_size=4096):
    """records: list of (key_bytes, record_bytes). Returns node bytes."""
    desc = u32(0) + u32(0) + struct.pack(">b", -1) + b"\x01" \
        + u16(len(records)) + u16(0)
    body = bytearray(desc)
    offsets = []
    for key, rec in records:
        offsets.append(len(body))
        body += key + rec
    # offset array at end of node
    n = len(records)
    while len(body) < node_size - 2 * n:
        body += b"\x00"
    # Offset array grows BACKWARD from end of node: offset[0] occupies the
    # final 2 bytes, so write them in reverse.
    for off in reversed(offsets):
        body += u16(off)
    assert len(body) == node_size, len(body)
    return bytes(body)


def build_header_node(node_size=4096, leaf_records=6, total_nodes=2,
                      first_leaf=1):
    desc = u32(0) + u32(0) + struct.pack(">b", 1) + b"\x01" \
        + u16(0) + u16(0)
    bth = (u16(1)            # treeDepth
           + u32(1)          # rootNode
           + u32(leaf_records)
           + u32(first_leaf)  # firstLeafNode
           + u32(first_leaf)  # lastLeafNode
           + u16(node_size)  # nodeSize
           + u16(512)        # maxKeyLength
           + u32(total_nodes)
           + u32(0)          # freeNodes
           + u16(0)          # reserved1
           + u32(4096)       # clumpSize
           + b"\x00"         # btreeType
           + b"\xbc"         # keyCompareType = case-insensitive
           + u32(0)          # attributes
           + b"\x00" * 64)   # reserved3
    node = bytearray(desc + bth)
    node += b"\x00" * (node_size - len(node))
    return bytes(node)


def fork_data(logical_size, start_block, block_count):
    extents = u32(start_block) + u32(block_count) + b"\x00" * 56
    return u64(logical_size) + u32(16384) + u32(block_count) + extents


def build_volume_header(file_count=1, folder_count=2,
                        cat_start=2, cat_blocks=2,
                        ext_start=0, ext_blocks=0,
                        journal_info_block=0, attributes=0x0100):
    h = bytearray(512)
    h[0:2] = u16(0x482B)          # signature 'H+'
    h[2:4] = u16(4)               # version
    h[4:8] = u32(attributes)      # attributes (0x0100 clean; +0x2000 journaled)
    h[12:16] = u32(journal_info_block)
    h[32:36] = u32(file_count)
    h[36:40] = u32(folder_count)
    h[40:44] = u32(BLOCK_SIZE)    # blockSize
    h[44:48] = u32(TOTAL_BLOCKS)  # totalBlocks
    h[48:52] = u32(TOTAL_BLOCKS - 5)  # freeBlocks
    h[56:60] = u32(16384)         # rsrcClumpSize
    h[60:64] = u32(16384)         # dataClumpSize
    h[64:68] = u32(18)            # nextCatalogID
    # fork data slots
    h[112:192] = fork_data(0, 0, 0)                    # allocation
    h[192:272] = fork_data(ext_blocks * BLOCK_SIZE, ext_start,
                           ext_blocks)                # extents
    h[272:352] = fork_data(cat_blocks * BLOCK_SIZE, cat_start,
                           cat_blocks)                # catalog
    h[352:432] = fork_data(0, 0, 0)                    # attributes
    h[432:512] = fork_data(0, 0, 0)                    # startup
    return bytes(h)


def build_image():
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)

    # volume header at 1024
    img[1024:1536] = build_volume_header()

    # catalog leaf records (sorted by (parentID, name))
    records = [
        (catalog_key(1, ""), folder_record(2, valence=1)),
        (catalog_key(2, ""), thread_record(0x0003, 1, "")),
        (catalog_key(2, "Docs"), folder_record(16, valence=1)),
        (catalog_key(16, ""), thread_record(0x0003, 2, "Docs")),
        (catalog_key(16, "hello.txt"),
         file_record(17, 4, len(FILE_CONTENT))),
        (catalog_key(17, ""), thread_record(0x0004, 16, "hello.txt")),
    ]
    leaf = build_leaf_node(records)
    header_node = build_header_node()

    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf

    # file data at block 4
    data_off = 1024 + 4 * BLOCK_SIZE
    img[data_off: data_off + len(FILE_CONTENT)] = FILE_CONTENT
    return bytes(img)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test-hfs.img"
    img = build_image()
    with open(out, "wb") as f:
        f.write(img)
    print("wrote %d bytes to %s" % (len(img), out))


# ---------------------------------------------------------------------------
# G2 fixture: fragmented file spilling into the extents-overflow B-tree
# ---------------------------------------------------------------------------

def _fill_block(img, block_num, pattern):
    off = 1024 + block_num * BLOCK_SIZE
    img[off: off + BLOCK_SIZE] = (pattern * BLOCK_SIZE)[:BLOCK_SIZE]


def build_overflow_image():
    """big.bin: 8 inline extents (blocks 10-17) + 4 overflow (blocks 20-23).

    Returns (image_bytes, expected_file_bytes, file_id).
    """
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)
    img[1024:1536] = build_volume_header(file_count=1, folder_count=2,
                                         cat_start=2, cat_blocks=2,
                                         ext_start=5, ext_blocks=2)

    inline = [(10 + i, 1) for i in range(8)]
    logical = 12 * BLOCK_SIZE
    records = [
        (catalog_key(1, ""), folder_record(2, valence=1)),
        (catalog_key(2, ""), thread_record(0x0003, 1, "")),
        (catalog_key(2, "Docs"), folder_record(16, valence=1)),
        (catalog_key(16, ""), thread_record(0x0003, 2, "Docs")),
        (catalog_key(16, "big.bin"),
         file_record_extents(18, inline, logical)),
        (catalog_key(18, ""), thread_record(0x0004, 16, "big.bin")),
    ]
    leaf = build_leaf_node(records)
    header_node = build_header_node(leaf_records=6)
    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf

    # Extents-overflow B-tree: header node (block 5) + leaf (block 6)
    # with one record: data fork of file 18 continues at logical block 8.
    okey = extent_key(0x00, 18, 8)
    orec = extent_record([(20, 1), (21, 1), (22, 1), (23, 1)])
    oleaf = build_leaf_node([(okey, orec)])
    ohead = build_header_node(leaf_records=1)
    ext_base = 1024 + 5 * BLOCK_SIZE
    img[ext_base: ext_base + 4096] = ohead
    img[ext_base + 4096: ext_base + 8192] = oleaf

    # Data blocks with per-block patterns
    expected = bytearray()
    for blk in list(range(10, 18)) + list(range(20, 24)):
        pat = b"BLK%02d|" % blk
        _fill_block(img, blk, pat)
        expected += (pat * BLOCK_SIZE)[:BLOCK_SIZE]
    return bytes(img), bytes(expected), 18


# ---------------------------------------------------------------------------
# G4 fixture: hard link -> iNode file in HFS+ Private Data
# ---------------------------------------------------------------------------

PRIVATE_DIR_NAME = "\x00\x00\x00\x00HFS+ Private Data"
HLINK_CONTENT = b"hardlink target content via iNode42\n"


def build_hardlink_image():
    """mylink.txt (flags 0x0020, hlnk/hfs+) -> iNode42's data."""
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)
    img[1024:1536] = build_volume_header(file_count=2, folder_count=3)

    records = [
        (1, "", catalog_key(1, ""),
         folder_record(2, valence=2)),
        (2, "", catalog_key(2, ""),
         thread_record(0x0003, 1, "")),
        (2, PRIVATE_DIR_NAME, catalog_key(2, PRIVATE_DIR_NAME),
         folder_record(19, valence=1)),
        (2, "Docs", catalog_key(2, "Docs"),
         folder_record(16, valence=1)),
        (16, "", catalog_key(16, ""),
         thread_record(0x0003, 2, "Docs")),
        (16, "mylink.txt", catalog_key(16, "mylink.txt"),
         file_record_extents(21, [], 0, flags=0x0020,
                             ftype=b"hlnk", fcreator=b"hfs+",
                             special=42)),
        (19, "", catalog_key(19, ""),
         thread_record(0x0003, 2, PRIVATE_DIR_NAME)),
        (19, "iNode42", catalog_key(19, "iNode42"),
         file_record_extents(20, [(30, 1)], len(HLINK_CONTENT))),
        (21, "", catalog_key(21, ""),
         thread_record(0x0004, 16, "mylink.txt")),
        (20, "", catalog_key(20, ""),
         thread_record(0x0004, 19, "iNode42")),
    ]
    # B-tree order: (parent_id, name) with binary UTF-16BE name compare
    records.sort(key=lambda t: (t[0], t[1].encode("utf-16-be")))
    kv = [(k, r) for _pid, _name, k, r in records]
    leaf = build_leaf_node(kv)
    header_node = build_header_node(leaf_records=len(kv))
    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf

    _fill_block(img, 30, HLINK_CONTENT)
    return bytes(img)


# ---------------------------------------------------------------------------
# G3 fixture: file with extended-attributes flag (maybe decmpfs)
# ---------------------------------------------------------------------------

def build_compressed_image():
    """One file with flags 0x0004 -> extract_file must refuse."""
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)
    img[1024:1536] = build_volume_header(file_count=1, folder_count=1)
    records = [
        (catalog_key(1, ""), folder_record(2, valence=1)),
        (catalog_key(2, ""), thread_record(0x0003, 1, "")),
        (catalog_key(2, "comp.dat"),
         file_record_extents(16, [(4, 1)], 16, flags=0x0004)),
        (catalog_key(16, ""), thread_record(0x0004, 2, "comp.dat")),
    ]
    leaf = build_leaf_node(records)
    header_node = build_header_node(leaf_records=len(records))
    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf
    _fill_block(img, 4, b"FAKECOMPRESSED!!")
    return bytes(img)


# ---------------------------------------------------------------------------
# Journal builders (G6): JournalInfoBlock + journal_header per TN1150
# ---------------------------------------------------------------------------

def journal_info_block(offset, size, flags=0x00000001):
    """180-byte JournalInfoBlock: flags, device_signature[8], offset, size."""
    b = bytearray(180)
    b[0:4] = u32(flags)            # kJIJournalInFSMask
    b[36:44] = u64(offset)         # byte offset of journal header
    b[44:52] = u64(size)           # journal size incl. header + buffer
    return bytes(b)


def journal_header(start, end, size, blhdr_size=4096, jhdr_size=512,
                   checksum=0):
    """44-byte journal_header: magic 'JNLx', endian, start, end, size..."""
    return (u32(0x4A4E4C78) + u32(0x12345678) + u64(start) + u64(end)
            + u64(size) + u32(blhdr_size) + u32(checksum) + u32(jhdr_size))


# ---------------------------------------------------------------------------
# G5 fixture: two files claiming the same allocation block(s)
# ---------------------------------------------------------------------------

def build_crosslink_image(crosslinked=True):
    """a.txt + b.txt in /Docs; both claim block 4 iff crosslinked.

    Returns image bytes. When crosslinked=False, b.txt claims block 5
    instead and detect_crosslinks() must return [].
    """
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)
    img[1024:1536] = build_volume_header(file_count=2, folder_count=2)
    b_block = 4 if crosslinked else 5
    records = [
        (catalog_key(1, ""), folder_record(2, valence=1)),
        (catalog_key(2, ""), thread_record(0x0003, 1, "")),
        (catalog_key(2, "Docs"), folder_record(16, valence=1)),
        (catalog_key(16, ""), thread_record(0x0003, 2, "Docs")),
        (catalog_key(16, "a.txt"),
         file_record_extents(17, [(4, 1)], 11)),
        (catalog_key(17, ""), thread_record(0x0004, 16, "a.txt")),
        (catalog_key(16, "b.txt"),
         file_record_extents(18, [(b_block, 1)], 11)),
        (catalog_key(18, ""), thread_record(0x0004, 16, "b.txt")),
    ]
    leaf = build_leaf_node(records)
    header_node = build_header_node(leaf_records=len(records))
    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf
    _fill_block(img, 4, b"AAA-crosslink")
    _fill_block(img, 5, b"BBB-clean")
    return bytes(img)


# ---------------------------------------------------------------------------
# G6 fixture: journaled volume with a synthetic journal
# ---------------------------------------------------------------------------

# Journal layout in the fixture: info block in allocation block 10,
# journal header + buffer in blocks 11-12 (8192 bytes).
JOURNAL_INFO_BLOCK_NUM = 10
JOURNAL_BLOCK_NUM = 11
JOURNAL_SIZE = 2 * BLOCK_SIZE


def build_journal_image(non_empty=True, clean=True):
    """Journaled volume (hello.txt catalog) + synthetic journal.

    non_empty: journal header start != end (transactions pending).
    clean: volume header clean-unmount bit set.
    Returns (image_bytes, journal_byte_offset).
    """
    size = 1024 + TOTAL_BLOCKS * BLOCK_SIZE
    img = bytearray(size)
    attrs = 0x2100 if clean else 0x2000  # journaled (+ clean unmount)
    img[1024:1536] = build_volume_header(
        journal_info_block=JOURNAL_INFO_BLOCK_NUM, attributes=attrs)
    records = [
        (catalog_key(1, ""), folder_record(2, valence=1)),
        (catalog_key(2, ""), thread_record(0x0003, 1, "")),
        (catalog_key(2, "Docs"), folder_record(16, valence=1)),
        (catalog_key(16, ""), thread_record(0x0003, 2, "Docs")),
        (catalog_key(16, "hello.txt"),
         file_record(17, 4, len(FILE_CONTENT))),
        (catalog_key(17, ""), thread_record(0x0004, 16, "hello.txt")),
    ]
    leaf = build_leaf_node(records)
    header_node = build_header_node()
    cat_base = 1024 + 2 * BLOCK_SIZE
    img[cat_base: cat_base + 4096] = header_node
    img[cat_base + 4096: cat_base + 8192] = leaf
    data_off = 1024 + 4 * BLOCK_SIZE
    img[data_off: data_off + len(FILE_CONTENT)] = FILE_CONTENT

    joff = 1024 + JOURNAL_BLOCK_NUM * BLOCK_SIZE
    # Journal info block at block 10
    img[1024 + JOURNAL_INFO_BLOCK_NUM * BLOCK_SIZE:
        1024 + JOURNAL_INFO_BLOCK_NUM * BLOCK_SIZE + 180] = \
        journal_info_block(joff, JOURNAL_SIZE)
    # Journal header: empty iff start == end
    start = 512
    end = 512 + 4096 if non_empty else 512
    img[joff: joff + 44] = journal_header(start, end, JOURNAL_SIZE)
    return bytes(img), joff


# ---------------------------------------------------------------------------
# G7 fixture: corrupt primary header, valid alternate header
# ---------------------------------------------------------------------------

def build_alternate_header_image(corrupt_primary=True):
    """build_image() + a valid header copy at total_size - 1024.

    With corrupt_primary=True the primary signature is zeroed, so
    HFSVolume must fall back to the alternate. Returns image bytes.
    """
    img = bytearray(build_image())
    total = len(img)
    img[total - 1024: total - 1024 + 512] = img[1024:1536]
    if corrupt_primary:
        img[1024:1026] = b"\x00\x00"
    return bytes(img)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
hfs_parse.py -- read-only HFS+ parsers.

Parses: volume header, fork data, B-tree node descriptors, B-tree header
records, catalog keys, and catalog folder / file / thread records.
Also: cross-linked extent detection (G5), journal assessment
(read-only, no replay; G6), alternate volume header fallback (G7).

All integers are big-endian. Nothing here writes to any device; callers
pass in bytes they read themselves.

Reference: Apple's TN1150 (structure layouts); this module is an original
implementation.
"""

import struct
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOLUME_HEADER_OFFSET = 1024
VOLUME_HEADER_SIZE = 512

SIG_HFSPLUS = 0x482B   # 'H+'
SIG_HFSX = 0x4858      # 'HX'

# Catalog record types
REC_FOLDER = 0x0001
REC_FILE = 0x0002
REC_FOLDER_THREAD = 0x0003
REC_FILE_THREAD = 0x0004

# B-tree node kinds
NODE_LEAF = -1
NODE_INDEX = 0
NODE_HEADER = 1
NODE_MAP = 2

# Reserved catalog node IDs
CNID_ROOT_PARENT = 1
CNID_ROOT_FOLDER = 2
CNID_EXTENTS_FILE = 3
CNID_CATALOG_FILE = 4
CNID_BAD_BLOCK_FILE = 5
CNID_ALLOCATION_FILE = 6
CNID_STARTUP_FILE = 7
CNID_ATTR_FILE = 8
CNID_FIRST_USER = 16

# Mac absolute time (1904-01-01 GMT) -> Unix epoch delta
MAC_TO_UNIX = 2082844800

# Fork types for extents-overflow keys (TN1150)
FORK_DATA = 0x00
FORK_RSRC = 0xFF

# Catalog file-record flag bits (TN1150)
K_HFS_HAS_ATTRIBUTES = 0x0004  # extended attributes present; may be decmpfs
K_HFS_HAS_LINK_CHAIN = 0x0020  # hard link (verify Finder type/creator too)

# Volume-header attribute bits (TN1150)
K_HFS_VOLUME_JOURNALED = 0x2000  # kHFSVolumeJournaledBit
K_HFS_VOLUME_CLEAN_UNMOUNT = 0x0100  # kHFSVolumeUnmountedBit (clean unmount)

# Journal (TN1150 "Journal" section; xnu vfs_journal.h)
JOURNAL_HEADER_MAGIC = 0x4A4E4C78  # 'JNLx'
JOURNAL_ENDIAN_MAGIC = 0x12345678
JOURNAL_INFO_BLOCK_SIZE = 180  # flags + device_signature[8] + offset + size + reserved[32]
JOURNAL_HEADER_SIZE = 44  # magic..jhdr_size (excludes newer sequence_num)
K_JI_JOURNAL_IN_FS = 0x00000001
K_JI_JOURNAL_ON_OTHER_DEVICE = 0x00000002
K_JI_JOURNAL_NEED_INIT = 0x00000004

# Alternate volume header: backup copy starts 1024 bytes before end of volume
ALTERNATE_HEADER_FROM_END = 1024

# Hard-link Finder type/creator and private-data directory name (TN1150)
HLINK_TYPE = b"hlnk"
HLINK_CREATOR = b"hfs+"
PRIVATE_DATA_DIR = "\x00\x00\x00\x00HFS+ Private Data"


class CompressedFileError(Exception):
    """File may be decmpfs-compressed; raw fork extraction is unsafe."""

RECORD_TYPE_NAMES = {
    REC_FOLDER: "folder",
    REC_FILE: "file",
    REC_FOLDER_THREAD: "folder_thread",
    REC_FILE_THREAD: "file_thread",
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def u16(b, off=0):
    return struct.unpack_from(">H", b, off)[0]


def u32(b, off=0):
    return struct.unpack_from(">I", b, off)[0]


def u64(b, off=0):
    return struct.unpack_from(">Q", b, off)[0]


def s8(b, off=0):
    return struct.unpack_from(">b", b, off)[0]


def mac_time_to_unix(mac_time):
    """Convert Mac absolute time to Unix timestamp. 0 stays 0 (unset)."""
    if mac_time == 0:
        return 0
    return mac_time - MAC_TO_UNIX


def mac_time_to_iso(mac_time):
    """Convert Mac absolute time to ISO-8601 string (UTC)."""
    if mac_time == 0:
        return None
    try:
        return datetime.fromtimestamp(mac_time_to_unix(mac_time),
                                      tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def decode_hfs_unistr(data, off):
    """Parse HFSUniStr255 at off. Returns (name_str, total_bytes)."""
    length = u16(data, off)
    raw = data[off + 2: off + 2 + length * 2]
    try:
        name = raw.decode("utf-16-be")
    except UnicodeDecodeError:
        name = raw.decode("utf-16-be", errors="replace")
    return name, 2 + length * 2


# ---------------------------------------------------------------------------
# Volume header
# ---------------------------------------------------------------------------

_VOLUME_HEADER_FIELDS = [
    ("signature", 0, ">H"),
    ("version", 2, ">H"),
    ("attributes", 4, ">I"),
    ("last_mounted_version", 8, ">I"),
    ("journal_info_block", 12, ">I"),
    ("create_date", 16, ">I"),
    ("modify_date", 20, ">I"),
    ("backup_date", 24, ">I"),
    ("checked_date", 28, ">I"),
    ("file_count", 32, ">I"),
    ("folder_count", 36, ">I"),
    ("block_size", 40, ">I"),
    ("total_blocks", 44, ">I"),
    ("free_blocks", 48, ">I"),
    ("next_allocation", 52, ">I"),
    ("rsrc_clump_size", 56, ">I"),
    ("data_clump_size", 60, ">I"),
    ("next_catalog_id", 64, ">I"),
    ("write_count", 68, ">I"),
    ("encodings_bitmap", 72, ">Q"),
]

_FORK_NAMES = ["allocation_file", "extents_file", "catalog_file",
               "attributes_file", "startup_file"]
_FORK_OFFSETS = [112, 192, 272, 352, 432]


def parse_fork_data(data, off=0):
    """Parse an 80-byte HFSPlusForkData. Returns dict with extents list."""
    logical_size = u64(data, off)
    clump_size = u32(data, off + 8)
    total_blocks = u32(data, off + 12)
    extents = []
    for i in range(8):
        start = u32(data, off + 16 + i * 8)
        count = u32(data, off + 16 + i * 8 + 4)
        if count:
            extents.append({"start_block": start, "block_count": count})
    return {
        "logical_size": logical_size,
        "clump_size": clump_size,
        "total_blocks": total_blocks,
        "extents": extents,
    }


def parse_volume_header(data, off=VOLUME_HEADER_OFFSET):
    """Parse the 512-byte HFS+ volume header. Raises ValueError if bad."""
    if len(data) < off + VOLUME_HEADER_SIZE:
        raise ValueError("buffer too small for volume header")
    hdr = {}
    for name, field_off, fmt in _VOLUME_HEADER_FIELDS:
        hdr[name] = struct.unpack_from(fmt, data, off + field_off)[0]
    hdr["finder_info"] = data[off + 80: off + 112]
    for name, fork_off in zip(_FORK_NAMES, _FORK_OFFSETS):
        hdr[name] = parse_fork_data(data, off + fork_off)
    hdr["is_hfsplus"] = hdr["signature"] in (SIG_HFSPLUS, SIG_HFSX)
    hdr["is_hfsx"] = hdr["signature"] == SIG_HFSX
    hdr["journaled"] = bool(hdr["attributes"] & K_HFS_VOLUME_JOURNALED)
    hdr["clean_unmount"] = bool(hdr["attributes"] & K_HFS_VOLUME_CLEAN_UNMOUNT)
    hdr["create_date_iso"] = mac_time_to_iso(hdr["create_date"])
    hdr["modify_date_iso"] = mac_time_to_iso(hdr["modify_date"])
    if not hdr["is_hfsplus"]:
        raise ValueError("not an HFS+ volume (bad signature 0x%04x)"
                         % hdr["signature"])
    return hdr


def block_to_offset(block_num, block_size):
    """Byte offset of an allocation block. Block 0 starts at byte 1024."""
    return VOLUME_HEADER_OFFSET + block_num * block_size


# ---------------------------------------------------------------------------
# B-tree nodes
# ---------------------------------------------------------------------------

def parse_btnode_descriptor(data, off=0):
    """Parse the 14-byte BTNodeDescriptor."""
    if len(data) < off + 14:
        raise ValueError("buffer too small for node descriptor")
    return {
        "flink": u32(data, off),
        "blink": u32(data, off + 4),
        "kind": s8(data, off + 8),
        "height": data[off + 9],
        "num_records": u16(data, off + 10),
        "reserved": u16(data, off + 12),
    }


def parse_btheader_rec(data, off=0):
    """Parse the 106-byte BTHeaderRec (in node 0 after descriptor)."""
    if len(data) < off + 106:
        raise ValueError("buffer too small for B-tree header record")
    return {
        "tree_depth": u16(data, off),
        "root_node": u32(data, off + 2),
        "leaf_records": u32(data, off + 6),
        "first_leaf_node": u32(data, off + 10),
        "last_leaf_node": u32(data, off + 14),
        "node_size": u16(data, off + 18),
        "max_key_length": u16(data, off + 20),
        "total_nodes": u32(data, off + 22),
        "free_nodes": u32(data, off + 26),
        "reserved1": u16(data, off + 30),
        "clump_size": u32(data, off + 32),
        "btree_type": data[off + 36],
        "key_compare_type": data[off + 37],
        "attributes": u32(data, off + 38),
    }


def node_record_offsets(node_data, node_size, num_records):
    """Record start offsets from the trailing offset array."""
    offs = []
    for i in range(num_records):
        offs.append(u16(node_data, node_size - 2 * (i + 1)))
    return offs


def iter_node_records(node_data, node_size):
    """Yield (key_bytes, record_data) for each record in a B-tree node."""
    desc = parse_btnode_descriptor(node_data)
    n = desc["num_records"]
    if n == 0:
        return
    offs = node_record_offsets(node_data, node_size, n)
    for i, rec_off in enumerate(offs):
        if rec_off + 2 > node_size:
            raise ValueError("record offset out of range")
        key_len = u16(node_data, rec_off)
        key_end = rec_off + 2 + key_len
        if i + 1 < n:
            rec_end = offs[i + 1]
        else:
            rec_end = node_size - 2 * n
        if key_end > rec_end or key_end > node_size:
            raise ValueError("record overruns node bounds")
        yield node_data[rec_off: key_end], node_data[key_end: rec_end]


# ---------------------------------------------------------------------------
# Catalog keys and records
# ---------------------------------------------------------------------------

def parse_catalog_key(key_bytes):
    """Parse an HFS+ catalog key (without the 2-byte length prefix)."""
    if len(key_bytes) < 6:
        raise ValueError("catalog key too short")
    parent_id = u32(key_bytes, 0)
    name, _ = decode_hfs_unistr(key_bytes, 4)
    return {"parent_id": parent_id, "name": name}


def parse_bsd_info(data, off=0):
    """Parse 16-byte HFSPlusBSDInfo."""
    return {
        "owner_id": u32(data, off),
        "group_id": u32(data, off + 4),
        "admin_flags": data[off + 8],
        "owner_flags": data[off + 9],
        "file_mode": u16(data, off + 10),
        "special": u32(data, off + 12),
    }


def _parse_dates(data, off):
    names = ["create_date", "content_mod_date", "attribute_mod_date",
             "access_date", "backup_date"]
    out = {}
    for i, name in enumerate(names):
        raw = u32(data, off + i * 4)
        out[name] = raw
        out[name + "_iso"] = mac_time_to_iso(raw)
    return out, off + 20


def parse_catalog_folder_record(data):
    """Parse 88-byte folder record (record data, no key). Per TN1150."""
    if len(data) < 88:
        raise ValueError("folder record too short")
    rec = {"record_type": u16(data, 0), "type_name": "folder"}
    rec["flags"] = u16(data, 2)
    rec["valence"] = u32(data, 4)
    rec["folder_id"] = u32(data, 8)
    dates, off = _parse_dates(data, 12)
    rec.update(dates)
    rec["bsd_info"] = parse_bsd_info(data, off)
    # G1 fix: 32 bytes of Finder info (DInfo + DXInfo) after bsd_info
    rec["finder_info"] = data[off + 16: off + 48]
    rec["text_encoding"] = u32(data, off + 48)
    rec["reserved"] = u32(data, off + 52)
    return rec


def parse_catalog_file_record(data):
    """Parse 248-byte file record (record data, no key). Per TN1150."""
    if len(data) < 248:
        raise ValueError("file record too short")
    rec = {"record_type": u16(data, 0), "type_name": "file"}
    rec["flags"] = u16(data, 2)
    rec["reserved1"] = u32(data, 4)
    rec["file_id"] = u32(data, 8)
    dates, off = _parse_dates(data, 12)
    rec.update(dates)
    rec["bsd_info"] = parse_bsd_info(data, off)
    # G1 fix: 32 bytes of Finder info (FInfo + FXInfo) after bsd_info
    rec["finder_info"] = data[off + 16: off + 48]
    rec["text_encoding"] = u32(data, off + 48)
    rec["reserved2"] = u32(data, off + 52)
    rec["data_fork"] = parse_fork_data(data, off + 56)
    rec["resource_fork"] = parse_fork_data(data, off + 136)
    return rec


def parse_catalog_thread_record(data):
    """Parse a folder/file thread record (variable length)."""
    if len(data) < 10:
        raise ValueError("thread record too short")
    rtype = u16(data, 0)
    name, _ = decode_hfs_unistr(data, 8)
    return {
        "record_type": rtype,
        "type_name": RECORD_TYPE_NAMES.get(rtype, "unknown"),
        "reserved": u16(data, 2),
        "parent_id": u32(data, 4),
        "node_name": name,
    }


def parse_catalog_record(data):
    """Dispatch on record type. Returns dict with type_name."""
    if len(data) < 2:
        raise ValueError("record too short for type field")
    rtype = u16(data, 0)
    if rtype == REC_FOLDER:
        return parse_catalog_folder_record(data)
    if rtype == REC_FILE:
        return parse_catalog_file_record(data)
    if rtype in (REC_FOLDER_THREAD, REC_FILE_THREAD):
        return parse_catalog_thread_record(data)
    raise ValueError("unknown catalog record type 0x%04x" % rtype)


# ---------------------------------------------------------------------------
# Extents overflow B-tree (G2)
# ---------------------------------------------------------------------------

def parse_extent_key(key_bytes):
    """Parse an HFSPlusExtentKey body (10 bytes, no length prefix).

    Layout: forkType u8, pad u8, fileID u32, startBlock u32.
    startBlock is the logical block number within the fork.
    """
    if len(key_bytes) < 10:
        raise ValueError("extent key too short")
    return {
        "fork_type": key_bytes[0],
        "file_id": u32(key_bytes, 2),
        "start_block": u32(key_bytes, 6),
    }


def parse_extent_record(rec_data):
    """Parse up to 8 HFSPlusExtentDescriptors (64 bytes)."""
    if len(rec_data) < 64:
        raise ValueError("extent record too short")
    extents = []
    for i in range(8):
        start = u32(rec_data, i * 8)
        count = u32(rec_data, i * 8 + 4)
        if count:
            extents.append({"start_block": start, "block_count": count})
    return extents


def get_fork_extents(volume, fork_data, file_id, fork_type):
    """All extents of a fork: inline extents plus extents-overflow
    records for (fork_type, file_id), ordered by logical start block.

    Shared by read_fork_full() (G2) and detect_crosslinks() (G5) so
    both see the same extent list.
    """
    extents = list(fork_data["extents"])
    logical = fork_data["logical_size"]
    covered = sum(e["block_count"] for e in extents) * volume.block_size
    if covered < logical:
        # Chain overflow records for this (fork_type, file_id),
        # ordered by logical start block.
        extra = []
        for key, oext in volume.iter_extents_overflow_records():
            if (key["fork_type"] == fork_type
                    and key["file_id"] == file_id):
                extra.append((key["start_block"], oext))
        extra.sort(key=lambda t: t[0])
        for _start_block, oext in extra:
            extents.extend(oext)
    return extents


def read_fork_full(volume, fork_data, file_id, fork_type):
    """Read a fork's bytes, following the extents-overflow B-tree (G2).

    `volume` is an HFSVolume, `fork_data` a parse_fork_data() dict,
    `fork_type` is FORK_DATA or FORK_RSRC.
    """
    extents = get_fork_extents(volume, fork_data, file_id, fork_type)
    logical = fork_data["logical_size"]
    out = bytearray()
    for e in extents:
        if len(out) >= logical:
            break
        chunk = volume._read(block_to_offset(e["start_block"],
                                             volume.block_size),
                             e["block_count"] * volume.block_size)
        out += chunk
    return bytes(out[:logical])


# ---------------------------------------------------------------------------
# Hard links (G4) and decmpfs detection (G3)
# ---------------------------------------------------------------------------

def is_compressed(file_record):
    """True if the file has extended attributes and may be decmpfs-
    compressed. Raw fork extraction is unsafe for such files (G3)."""
    return bool(file_record.get("flags", 0) & K_HFS_HAS_ATTRIBUTES)


def resolve_hardlink(volume, file_record):
    """Resolve an HFS+ hard link to its indirect-node file record (G4).

    Returns the iNode file's record dict, or None if `file_record`
    is not a hard link (or the iNode cannot be found).
    """
    if not (file_record.get("flags", 0) & K_HFS_HAS_LINK_CHAIN):
        return None
    fi = file_record.get("finder_info", b"")
    if len(fi) < 8 or fi[0:4] != HLINK_TYPE or fi[4:8] != HLINK_CREATOR:
        return None
    inode = file_record["bsd_info"]["special"]
    idx = volume.build_name_index()
    priv = idx.get((CNID_ROOT_FOLDER, PRIVATE_DATA_DIR))
    if priv is None or priv.get("type_name") != "folder":
        return None
    target = idx.get((priv["folder_id"], "iNode%d" % inode))
    if target is None or target.get("type_name") != "file":
        return None
    return target


# ---------------------------------------------------------------------------
# Cross-linked extents (G5)
# ---------------------------------------------------------------------------

def detect_crosslinks(volume):
    """Flag allocation blocks claimed by more than one file (G5).

    Builds an interval map of every harvested file's extents (data
    fork + resource fork, including extents-overflow records via G2)
    and reports overlaps. Legitimate hard-link sharing (G4) is
    excluded: all records resolving to the same iNode count as one
    claimant, and every path resolving to that inode is listed for it
    (private-data paths are dropped when a user-visible path exists).
    A file overlapping its own extents is not a cross-link.

    Returns a list of ((start_block, end_block_exclusive), [paths]),
    sorted by start block, with paths sorted. `end_block_exclusive`
    is one past the last shared block, so the shared range is
    start_block .. end_block_exclusive - 1. Empty list = clean.
    """
    threads = volume.build_thread_index()
    # One claimant per resolved file id: {file_id: [paths]}.
    claimants = {}
    claimed_fids = set()
    intervals = []  # (start, end_exclusive, file_id)
    for _key, rec in volume.iter_catalog_records():
        if rec.get("type_name") != "file":
            continue
        target = resolve_hardlink(volume, rec) or rec
        fid = target.get("file_id")
        if fid is None:
            continue
        cnid = rec.get("file_id")
        path = volume.resolve_path(cnid, threads) if cnid is not None else None
        paths = claimants.setdefault(fid, [])
        if path is not None and path not in paths:
            paths.append(path)
        if fid in claimed_fids:
            # Already claimed this inode's extents (hard-link twin);
            # record the extra path above, but do not double-count.
            continue
        claimed_fids.add(fid)
        for fork, fork_type in ((target["data_fork"], FORK_DATA),
                                (target["resource_fork"], FORK_RSRC)):
            for e in get_fork_extents(volume, fork, fid, fork_type):
                if e["block_count"] <= 0:
                    continue
                intervals.append((e["start_block"],
                                  e["start_block"] + e["block_count"],
                                  fid))
    if not intervals:
        return []
    # Boundary sweep: at every interval start/end the active claimant
    # set is constant between consecutive boundaries, so each
    # elementary segment gets exactly one claimant set. Segments
    # claimed by >1 file are cross-links. Adjacent segments with the
    # same claimant set merge into one range.
    events = []  # (block, is_start, file_id)
    for start, end, fid in intervals:
        events.append((start, True, fid))
        events.append((end, False, fid))
    # Ends processed before starts at the same block: [start, end)
    # intervals are half-open, so a block is never double-counted.
    events.sort(key=lambda t: (t[0], t[1]))
    active = {}  # file_id -> number of its intervals covering this point
    segments = []  # (start, end, frozenset(file_ids))
    prev = None
    for pos, is_start, fid in events:
        if prev is not None and pos > prev and len(active) > 1:
            segments.append((prev, pos, frozenset(active)))
        if is_start:
            active[fid] = active.get(fid, 0) + 1
        else:
            remaining = active.get(fid, 0) - 1
            if remaining > 0:
                active[fid] = remaining
            else:
                active.pop(fid, None)
        prev = pos
    if not segments:
        return []
    # Merge adjacent segments with identical claimant sets.
    merged = []
    for lo, hi, fids in segments:
        if merged and merged[-1][1] == lo and merged[-1][2] == fids:
            merged[-1] = (merged[-1][0], hi, fids)
        else:
            merged.append((lo, hi, fids))
    out = []
    for lo, hi, fids in merged:
        paths = set()
        for fid in fids:
            visible = [p for p in claimants.get(fid, [])
                       if "Private Data" not in p]
            paths.update(visible or claimants.get(fid, []))
        out.append(((lo, hi), sorted(paths)))
    return out


# ---------------------------------------------------------------------------
# Journal replay assessment, read-only (G6)
#
# Assessment only: this module NEVER replays the journal and NEVER
# writes blocks back to disk. A non-empty journal means the on-disk
# catalog may be stale and a replay (a separate sub-project, per
# GAPS.md G6) is needed before trusting the harvested catalog.
# ---------------------------------------------------------------------------

def parse_journal_info_block(data, off=0):
    """Parse a 180-byte JournalInfoBlock (TN1150).

    Layout: flags u32, device_signature[8] u32, offset u64, size u64,
    reserved[32] u32. `offset` is the byte offset from the start of
    the volume to the journal header; `size` covers the journal
    header + journal buffer (not the info block itself).
    """
    if len(data) < off + 52:
        raise ValueError("buffer too small for journal info block")
    flags = u32(data, off)
    return {
        "flags": flags,
        "device_signature": [u32(data, off + 4 + i * 4)
                             for i in range(8)],
        "offset": u64(data, off + 36),
        "size": u64(data, off + 44),
        "in_fs": bool(flags & K_JI_JOURNAL_IN_FS),
        "on_other_device": bool(flags & K_JI_JOURNAL_ON_OTHER_DEVICE),
        "need_init": bool(flags & K_JI_JOURNAL_NEED_INIT),
    }


def parse_journal_header(data, off=0):
    """Parse a 44-byte journal_header (TN1150).

    Layout: magic u32 ('JNLx'), endian u32, start u64, end u64,
    size u64, blhdr_size u32, checksum u32, jhdr_size u32.
    start/end are byte offsets from the start of the journal header;
    the journal is empty iff start == end.
    """
    if len(data) < off + JOURNAL_HEADER_SIZE:
        raise ValueError("buffer too small for journal header")
    return {
        "magic": u32(data, off),
        "endian": u32(data, off + 4),
        "start": u64(data, off + 8),
        "end": u64(data, off + 16),
        "size": u64(data, off + 24),
        "blhdr_size": u32(data, off + 32),
        "checksum": u32(data, off + 36),
        "jhdr_size": u32(data, off + 40),
    }


def check_journal(volume_data, vol):
    """Assess the journal of an HFS+ volume without replaying it (G6).

    `volume_data` is the whole volume image as bytes; `vol` is an
    HFSVolume or a parsed volume-header dict (from
    parse_volume_header()). Returns a dict:

      journal_present  -- journal_info_block != 0 and volume journaled
      journal_info_block / journal_offset / journal_size
      journal_header_valid -- magic 'JNLx', endian, size match JIB
      non_empty / needs_replay -- start != end in the journal header
      pending_bytes -- bytes of uncommitted transactions (0 if empty)
      unclean_unmount -- volume header's clean-unmount bit is clear
      need_init -- JIB says the journal header is invalid / uninit
      error -- human-readable problem, or None

    A journal on another device cannot be assessed from this image:
    journal_present is True but journal_header_valid is False and
    `error` explains why. The header checksum is NOT verified here
    (TN1150 calc_checksum); a bad magic/endian/size is enough to
    refuse to trust the header.
    """
    if hasattr(vol, "header"):
        header = vol.header
    elif isinstance(vol, dict):
        header = vol
    else:
        raise TypeError("vol must be an HFSVolume or a header dict")
    result = {
        "journal_present": False,
        "journaled": bool(header.get("journaled", False)),
        "journal_info_block": header.get("journal_info_block", 0),
        "journal_offset": None,
        "journal_size": None,
        "journal_header_valid": False,
        "header_magic_ok": False,
        "header_endian_ok": False,
        "size_matches_info_block": False,
        "need_init": False,
        "on_other_device": False,
        "in_fs": False,
        "non_empty": False,
        "needs_replay": False,
        "pending_bytes": 0,
        "journal_start": None,
        "journal_end": None,
        "unclean_unmount": not bool(header.get("clean_unmount", False)),
        "clean_unmount": bool(header.get("clean_unmount", False)),
        "error": None,
    }
    jib_block = header.get("journal_info_block", 0)
    if not jib_block:
        if result["journaled"]:
            result["error"] = ("volume claims to be journaled but "
                               "journal_info_block is 0")
        return result
    result["journal_present"] = True
    block_size = header.get("block_size", 0)
    if not block_size:
        result["error"] = "volume header has no block_size"
        return result
    jib_off = block_to_offset(jib_block, block_size)
    if jib_off + 52 > len(volume_data):
        result["error"] = ("journal info block %d is beyond the end "
                           "of the supplied image" % jib_block)
        return result
    try:
        jib = parse_journal_info_block(volume_data, jib_off)
    except ValueError as ex:
        result["error"] = "unparsable journal info block: %s" % ex
        return result
    result["journal_offset"] = jib["offset"]
    result["journal_size"] = jib["size"]
    result["need_init"] = jib["need_init"]
    result["on_other_device"] = jib["on_other_device"]
    result["in_fs"] = jib["in_fs"]
    if jib["need_init"]:
        result["error"] = ("journal header needs initialisation "
                           "(kJIJournalNeedInitMask); no valid "
                           "transactions in the journal")
        return result
    if jib["on_other_device"] and not jib["in_fs"]:
        result["error"] = ("journal lives on another device; its "
                           "header is not in this image")
        return result
    joff, jsize = jib["offset"], jib["size"]
    if joff + JOURNAL_HEADER_SIZE > len(volume_data):
        result["error"] = ("journal header at byte %d is beyond the "
                           "end of the supplied image" % joff)
        return result
    try:
        jhdr = parse_journal_header(volume_data, joff)
    except ValueError as ex:
        result["error"] = "unparsable journal header: %s" % ex
        return result
    result["header_magic_ok"] = jhdr["magic"] == JOURNAL_HEADER_MAGIC
    result["header_endian_ok"] = jhdr["endian"] == JOURNAL_ENDIAN_MAGIC
    result["size_matches_info_block"] = jhdr["size"] == jsize
    result["journal_header_valid"] = (result["header_magic_ok"]
                                      and result["header_endian_ok"]
                                      and result["size_matches_info_block"])
    if not result["journal_header_valid"]:
        result["error"] = ("journal header invalid: magic 0x%08x "
                           "(want 0x%08x), endian 0x%08x, "
                           "header size %d vs info-block size %d"
                           % (jhdr["magic"], JOURNAL_HEADER_MAGIC,
                              jhdr["endian"], jhdr["size"], jsize))
        return result
    result["journal_start"] = jhdr["start"]
    result["journal_end"] = jhdr["end"]
    result["non_empty"] = jhdr["start"] != jhdr["end"]
    result["needs_replay"] = result["non_empty"]
    if result["non_empty"] and jsize:
        # Circular buffer: pending bytes wrap at the journal size.
        result["pending_bytes"] = (jhdr["end"] - jhdr["start"]) % jsize
    return result


# ---------------------------------------------------------------------------
# Alternate volume header fallback (G7)
# ---------------------------------------------------------------------------

def try_alternate_header(volume_data, total_size=None):
    """Parse the backup volume header at total_size - 1024 (TN1150).

    `volume_data` is the whole volume image as bytes; `total_size`
    defaults to len(volume_data). Returns the parsed header dict.
    Raises ValueError if the image is too small, the alternate
    header lies beyond the supplied bytes, or its signature is bad.
    """
    if total_size is None:
        total_size = len(volume_data)
    if total_size < ALTERNATE_HEADER_FROM_END + VOLUME_HEADER_SIZE:
        raise ValueError("volume too small for an alternate header")
    off = total_size - ALTERNATE_HEADER_FROM_END
    if off + VOLUME_HEADER_SIZE > len(volume_data):
        raise ValueError("alternate header beyond supplied data")
    return parse_volume_header(volume_data, off)


# ---------------------------------------------------------------------------
# High-level read-only volume access
# ---------------------------------------------------------------------------

class HFSVolume:
    """Read-only access to an HFS+ volume image.

    `source` is a file path (opened read-only) or a bytes-like object.
    """

    def __init__(self, source):
        if isinstance(source, (bytes, bytearray, memoryview)):
            self._data = bytes(source)
            self._fh = None
        else:
            self._fh = open(source, "rb")  # read-only
            self._data = None
        try:
            raw = self._read(VOLUME_HEADER_OFFSET, VOLUME_HEADER_SIZE)
            self.header = parse_volume_header(raw, 0)
            self.header_source = "primary"
        except ValueError as primary_err:
            # G7: fall back to the backup header at end-of-volume - 1024.
            try:
                total = self._total_size()
                alt_raw = self._read(total - ALTERNATE_HEADER_FROM_END,
                                     VOLUME_HEADER_SIZE)
                self.header = parse_volume_header(alt_raw, 0)
                self.header_source = "alternate"
            except ValueError as alt_err:
                raise ValueError(
                    "primary volume header unreadable (%s); alternate "
                    "volume header unreadable (%s)"
                    % (primary_err, alt_err)) from None
        self.block_size = self.header["block_size"]
        cat = self.header["catalog_file"]
        self._catalog_extents = cat["extents"]
        self._catalog_size = cat["logical_size"]
        node0 = self._read_catalog_node(0)
        desc = parse_btnode_descriptor(node0)
        if desc["kind"] != NODE_HEADER:
            raise ValueError("catalog node 0 is not a header node")
        self.btree = parse_btheader_rec(node0, 14)
        self.node_size = self.btree["node_size"]
        self._name_index = None  # lazy (parent_id, name) -> record cache

    def _total_size(self):
        """Total volume-image size in bytes (bytes source or file)."""
        if self._data is not None:
            return len(self._data)
        self._fh.seek(0, 2)
        return self._fh.tell()

    def _read(self, offset, size):
        if self._data is not None:
            return self._data[offset: offset + size]
        self._fh.seek(offset)
        return self._fh.read(size)

    def _catalog_node_offset(self, node_num, node_size=None):
        """Byte offset of a catalog B-tree node number."""
        ns = node_size or self.node_size
        remaining = node_num * ns
        for e in self._catalog_extents:
            span = e["block_count"] * self.block_size
            if remaining < span:
                base = block_to_offset(e["start_block"], self.block_size)
                return base + remaining
            remaining -= span
        raise ValueError("catalog node %d out of range" % node_num)

    def _read_catalog_node(self, node_num):
        if hasattr(self, "btree"):
            ns = self.btree["node_size"]
        else:
            # First call (reading node 0 itself): probe node size from the
            # header record at its fixed position.
            probe = self._read(self._catalog_node_offset(node_num, 4096), 120)
            ns = parse_btheader_rec(probe, 14)["node_size"]
            if not (512 <= ns <= 32768):
                raise ValueError("catalog header node unreadable "
                                 "(bad node_size %d)" % ns)
        off = self._catalog_node_offset(node_num, ns)
        return self._read(off, ns)

    def iter_catalog_leaves(self):
        """Yield (node_num, node_bytes) for each leaf node in fLink order."""
        node_num = self.btree["first_leaf_node"]
        seen = set()
        while node_num != 0 and node_num not in seen:
            seen.add(node_num)
            node = self._read_catalog_node(node_num)
            yield node_num, node
            node_num = parse_btnode_descriptor(node)["flink"]

    def iter_catalog_records(self):
        """Yield (key_dict, record_dict) for every catalog record."""
        for _node_num, node in self.iter_catalog_leaves():
            try:
                records = list(iter_node_records(node, self.node_size))
            except ValueError:
                continue  # damaged node: harvest what we can
            for key_bytes, rec_data in records:
                try:
                    key = parse_catalog_key(key_bytes[2:])  # strip length
                    rec = parse_catalog_record(rec_data)
                except ValueError:
                    continue
                yield key, rec

    def _extents_node_offset(self, node_num, node_size):
        """Byte offset of an extents-overflow B-tree node number."""
        remaining = node_num * node_size
        for e in self.header["extents_file"]["extents"]:
            span = e["block_count"] * self.block_size
            if remaining < span:
                base = block_to_offset(e["start_block"], self.block_size)
                return base + remaining
            remaining -= span
        raise ValueError("extents node %d out of range" % node_num)

    def iter_extents_overflow_records(self):
        """Yield (key_dict, extents) from extents-overflow B-tree leaves."""
        if not self.header["extents_file"]["extents"]:
            return
        try:
            probe = self._read(self._extents_node_offset(0, 4096), 120)
            bth = parse_btheader_rec(probe, 14)
        except ValueError:
            return
        ns = bth["node_size"]
        if not (512 <= ns <= 32768):
            return
        node_num = bth["first_leaf_node"]
        seen = set()
        while node_num != 0 and node_num not in seen:
            seen.add(node_num)
            try:
                node = self._read(self._extents_node_offset(node_num, ns), ns)
                records = list(iter_node_records(node, ns))
            except ValueError:
                break
            for key_bytes, rec_data in records:
                try:
                    key = parse_extent_key(key_bytes[2:])  # strip length
                    exts = parse_extent_record(rec_data)
                except ValueError:
                    continue
                yield key, exts
            node_num = parse_btnode_descriptor(node)["flink"]

    def build_name_index(self):
        """Map (parent_id, name) -> file/folder record. Cached."""
        if self._name_index is None:
            idx = {}
            for key, rec in self.iter_catalog_records():
                if rec["type_name"] in ("file", "folder"):
                    idx[(key["parent_id"], key["name"])] = rec
            self._name_index = idx
        return self._name_index

    def extract_file(self, file_record):
        """Extract a file's data-fork bytes.

        Resolves hard links (G4), refuses possibly decmpfs-compressed
        files (G3), and follows the extents-overflow B-tree (G2).
        """
        if is_compressed(file_record):
            raise CompressedFileError(
                "file id %d has extended attributes; may be "
                "decmpfs-compressed; raw extraction refused"
                % file_record.get("file_id", -1))
        target = resolve_hardlink(self, file_record) or file_record
        fork = target["data_fork"]
        return read_fork_full(self, fork, target["file_id"], FORK_DATA)

    def build_thread_index(self):
        """Map CNID -> (parent_id, name) from thread records."""
        index = {}
        for key, rec in self.iter_catalog_records():
            if rec["type_name"] in ("folder_thread", "file_thread"):
                cnid = key["parent_id"]  # thread key's parentID == the CNID
                index[cnid] = (rec["parent_id"], rec["node_name"])
        return index

    def resolve_path(self, cnid, thread_index=None):
        """Resolve a CNID to a full path via thread records."""
        if thread_index is None:
            thread_index = self.build_thread_index()
        parts = []
        cur = cnid
        seen = set()
        while cur != CNID_ROOT_FOLDER and cur not in seen:
            seen.add(cur)
            entry = thread_index.get(cur)
            if entry is None:
                parts.append("<orphan:%d>" % cur)
                break
            parent, name = entry
            parts.append(name)
            cur = parent
        parts.reverse()
        return "/" + "/".join(parts)

    def read_fork(self, fork):
        """Read a fork's bytes via its extents, truncated to logical size."""
        out = bytearray()
        for e in fork["extents"]:
            if len(out) >= fork["logical_size"]:
                break
            chunk = self._read(block_to_offset(e["start_block"],
                                               self.block_size),
                               e["block_count"] * self.block_size)
            out += chunk
        return bytes(out[:fork["logical_size"]])

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# CLI: quick inspection
# ---------------------------------------------------------------------------

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Inspect an HFS+ volume (read-only)")
    ap.add_argument("image", help="path to raw HFS+ image")
    ap.add_argument("--list", action="store_true",
                    help="list all catalog records with resolved paths")
    ap.add_argument("--extract", metavar="PATH",
                    help="extract file at catalog path to stdout")
    ap.add_argument("--crosslinks", action="store_true",
                    help="report allocation blocks claimed by >1 file (G5)")
    args = ap.parse_args(argv)

    vol = HFSVolume(args.image)
    h = vol.header
    print("volume header: %s" % vol.header_source)
    print("signature : 0x%04x (%s)" % (h["signature"],
          "HFSX" if h["is_hfsx"] else "HFS+"))
    print("block size: %d" % h["block_size"])
    print("blocks    : %d total, %d free" % (h["total_blocks"], h["free_blocks"]))
    print("files     : %d  folders: %d" % (h["file_count"], h["folder_count"]))
    print("journaled : %s  clean unmount: %s" % (h["journaled"],
                                                h["clean_unmount"]))
    print("catalog   : %d bytes in %d extents, node size %d"
          % (vol._catalog_size, len(vol._catalog_extents), vol.node_size))

    if args.crosslinks:
        links = detect_crosslinks(vol)
        if not links:
            print("cross-links: none")
        for (start, end), paths in links:
            print("cross-linked blocks %d..%d: %s"
                  % (start, end - 1, ", ".join(paths)))
    if args.list or args.extract:
        threads = vol.build_thread_index()
        found = False
        for key, rec in vol.iter_catalog_records():
            t = rec["type_name"]
            if t in ("file", "folder"):
                cnid = rec.get("file_id", rec.get("folder_id"))
                path = vol.resolve_path(cnid, threads)
                if args.list:
                    marker = ""
                    if t == "file":
                        if is_compressed(rec):
                            marker += " [xattr:maybe-compressed]"
                        if resolve_hardlink(vol, rec) is not None:
                            marker += " [hardlink]"
                    print("%-6s %-10d %s%s" % (t, cnid, path, marker))
                if args.extract and path == args.extract and t == "file":
                    import sys
                    try:
                        sys.stdout.buffer.write(vol.extract_file(rec))
                    except CompressedFileError as ex:
                        sys.stderr.write("refusing to extract %s: %s\n"
                                         % (path, ex))
                        raise SystemExit(2)
                    found = True
                    break
        if args.extract and not found:
            raise SystemExit("path not found: %s" % args.extract)
    vol.close()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])

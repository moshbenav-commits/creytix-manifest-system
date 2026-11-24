#!/usr/bin/env python3
"""
scan_nodes.py -- DiskWarrior-style harvest primitive.

Sweeps raw bytes for plausible B-tree node descriptors. On a damaged volume
the catalog B-tree structure may be unreadable, but individual nodes often
survive intact in their old blocks. Finding them is step one of rebuilding.

A candidate at a node_size-aligned offset must satisfy:
  - kind byte in {-1, 0, 1, 2}
  - height byte small (<= 16)
  - num_records > 0 and small enough that descriptor + offsets fit
  - every trailing record offset points inside the node, >= 14,
    and offsets are strictly increasing

Each candidate is then validated by attempting to parse its records with
hfs_parse.iter_node_records. Only candidates whose records all parse are
reported as strong hits.

Usage:
    python3 scan_nodes.py <image> [--node-size 4096] [--step 4096]
"""

import argparse
import struct

from hfs_parse import (
    parse_btnode_descriptor, parse_btheader_rec, iter_node_records,
    NODE_LEAF, NODE_INDEX, NODE_HEADER, NODE_MAP,
)

VALID_KINDS = {NODE_LEAF, NODE_INDEX, NODE_HEADER, NODE_MAP}


def plausible_descriptor(data, off, node_size):
    """Cheap checks on a 14-byte node descriptor candidate."""
    if off + 14 > len(data):
        return None
    try:
        desc = parse_btnode_descriptor(data, off)
    except ValueError:
        return None
    kind = desc["kind"]
    n = desc["num_records"]
    if kind not in VALID_KINDS:
        return None
    if desc["height"] > 16:
        return None
    if n == 0 or n > 2000:
        return None
    # Descriptor + offset array must fit: 14 + 2*n <= node_size
    if 14 + 2 * n > node_size:
        return None
    # Offsets must be inside the node, >= 14, strictly increasing
    prev = 14
    for i in range(n):
        o = struct.unpack_from(">H", data, off + node_size - 2 * (i + 1))[0]
        if o < 14 or o >= node_size - 2 * n:
            return None
        if o < prev and i > 0:
            # allow equal? No: records can't start at same offset unless
            # zero-length, which is invalid. Require strictly increasing.
            return None
        if o <= prev and i > 0:
            return None
        prev = o
    return desc


def plausible_header_node(data, off, node_size):
    """Special-case check for B-tree header nodes (node 0).

    Header nodes don't use the normal record/offset layout; they carry a
    BTHeaderRec at +14. Validate that instead.
    """
    if off + 120 > len(data):
        return None
    try:
        desc = parse_btnode_descriptor(data, off)
        bth = parse_btheader_rec(data, off + 14)
    except ValueError:
        return None
    if desc["kind"] != NODE_HEADER:
        return None
    ns = bth["node_size"]
    if ns < 512 or ns > 32768 or ns != node_size:
        return None
    if bth["total_nodes"] == 0:
        return None
    return desc


def validate_node_records(data, off, node_size):
    """Try to parse every record; return count parsed or None on failure."""
    node = data[off: off + node_size]
    try:
        records = list(iter_node_records(node, node_size))
    except ValueError:
        return None
    return len(records)


def _scan_phases(data, node_size, block_size):
    """Byte phases (mod node_size) where B-tree nodes can start.

    Node N of a fork whose first extent starts at allocation block B sits
    at byte 1024 + B*blockSize + N*nodeSize. Allocation blocks begin at
    byte 1024, so the phase depends on blockSize. If the volume header is
    unreadable we fall back to the two most common phases.
    """
    import math
    if block_size is None:
        try:
            from hfs_parse import parse_volume_header, VOLUME_HEADER_OFFSET
            block_size = parse_volume_header(
                data, VOLUME_HEADER_OFFSET)["block_size"]
        except ValueError:
            block_size = None
    if block_size is None:
        return sorted({0, 1024 % node_size})
    g = math.gcd(block_size, node_size)
    phases = set()
    for b in range(node_size // g):
        phases.add((1024 + b * block_size) % node_size)
    return sorted(phases)


def scan(data, node_size=4096, block_size=None, validate=True):
    """Yield (offset, descriptor, record_count_or_None) for each hit."""
    seen = set()
    for phase in _scan_phases(data, node_size, block_size):
        for off in range(phase, len(data) - node_size + 1, node_size):
            if off in seen:
                continue
            seen.add(off)
            desc = plausible_header_node(data, off, node_size)
            if desc is not None:
                # Header node: no normal records to validate.
                yield off, desc, 0
                continue
            desc = plausible_descriptor(data, off, node_size)
            if desc is None:
                continue
            rec_count = (validate_node_records(data, off, node_size)
                         if validate else None)
            yield off, desc, rec_count


KIND_NAMES = {NODE_LEAF: "leaf", NODE_INDEX: "index",
              NODE_HEADER: "header", NODE_MAP: "map"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan raw image for B-tree nodes")
    ap.add_argument("image")
    ap.add_argument("--node-size", type=int, default=4096)
    ap.add_argument("--block-size", type=int, default=None,
                    help="allocation block size (default: read from volume header)")
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args(argv)

    with open(args.image, "rb") as f:  # read-only
        data = f.read()

    phases = _scan_phases(data, args.node_size, args.block_size)
    print("scanning %d bytes, node_size=%d phases=%s"
          % (len(data), args.node_size, phases))
    hits = 0
    for off, desc, rec_count in scan(data, args.node_size, args.block_size,
                                     validate=not args.no_validate):
        kind_name = KIND_NAMES.get(desc["kind"], str(desc["kind"]))
        status = ("records=%d" % rec_count) if rec_count is not None else "unvalidated"
        print("hit @ byte %-10d kind=%-6s num_records=%-5d %s"
              % (off, kind_name, desc["num_records"], status))
        hits += 1
    print("%d candidate node(s)" % hits)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Damage simulation: nuke the catalog B-tree header node, then harvest.

Simulates the CreytixVault2TB failure mode (catalog B-tree unreadable while
file data blocks survive). Shows the DiskWarrior-style recovery path:
  1. Normal B-tree walk fails (header node destroyed).
  2. Node-signature scan still finds the surviving leaf node.
  3. Records are harvested directly from the leaf bytes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_test_image import build_image
from hfs_parse import (HFSVolume, iter_node_records, parse_catalog_key,
                       parse_catalog_record)
from scan_nodes import scan

IMG = "/tmp/test-hfs-damaged.img"


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        sys.exit(1)


def main():
    img = bytearray(build_image())

    # Destroy catalog header node (node 0 at byte 9216) AND the volume
    # header's catalog fork pointer (simulate severe damage).
    cat_base = 1024 + 2 * 4096
    for i in range(cat_base, cat_base + 4096):
        img[i] = 0
    with open(IMG, "wb") as f:
        f.write(bytes(img))

    # 1. Normal walk fails
    try:
        vol = HFSVolume(IMG)
        list(vol.iter_catalog_records())
        walked = True
    except ValueError:
        walked = False
    check("normal B-tree walk fails on damaged catalog", not walked)

    # 2. Signature scan finds the surviving leaf node
    data = bytes(img)
    hits = [(off, desc) for off, desc, rc in scan(data, 4096)
            if rc is not None]
    leaf_hits = [off for off, desc in hits if desc["kind"] == -1]
    check("scanner finds surviving leaf node", leaf_hits == [cat_base + 4096])
    print("      leaf found at byte", leaf_hits[0] if leaf_hits else None)

    # 3. Harvest records straight from the leaf bytes
    harvested = []
    node = data[leaf_hits[0]: leaf_hits[0] + 4096]
    for key_bytes, rec_data in iter_node_records(node, 4096):
        try:
            key = parse_catalog_key(key_bytes[2:])
            rec = parse_catalog_record(rec_data)
            harvested.append((key, rec))
        except ValueError:
            continue
    check("6 records harvested from damaged volume", len(harvested) == 6)

    # 4. Rebuild paths from thread records (no B-tree needed)
    threads = {}
    for key, rec in harvested:
        if rec["type_name"] in ("folder_thread", "file_thread"):
            threads[key["parent_id"]] = (rec["parent_id"], rec["node_name"])

    def resolve(cnid):
        parts, cur, seen = [], cnid, set()
        while cur != 2 and cur not in seen:
            seen.add(cur)
            parent, name = threads[cur]
            parts.append(name)
            cur = parent
        parts.reverse()
        return "/" + "/".join(parts)

    paths = sorted(resolve(rec.get("file_id", rec.get("folder_id")))
                   for _k, rec in harvested
                   if rec["type_name"] in ("file", "folder"))
    check("paths rebuilt without B-tree", "/Docs/hello.txt" in paths)
    print("      rebuilt paths:", paths)

    # 5. File bytes still extractable: data fork extent -> block 4.
    #    (Volume header survived here; in the worst case block_size would
    #    come from the alternate header at end-of-volume - 1024.)
    for key, rec in harvested:
        if rec["type_name"] == "file":
            e = rec["data_fork"]["extents"][0]
            off = 1024 + e["start_block"] * 4096
            content = data[off: off + rec["data_fork"]["logical_size"]]
            check("file content recovered",
                  content.startswith(b"Hello, HFS+!"))
            print("      recovered %d bytes" % len(content))

    print("\nDAMAGE-SIMULATION TESTS PASSED")


if __name__ == "__main__":
    main()

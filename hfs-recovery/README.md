# HFS+ Recovery R&D

Reverse-engineering the core of DiskWarrior: rebuilding a damaged HFS+ catalog
from raw disk bytes. Read-only. This project never writes to a disk device.

References: Apple's TN1150 "HFS Plus Volume Format" (the canonical spec),
plus behavior notes from open-source HFS+ readers (hfsprust, dpp/hfsplus,
hfsplus-forensic, hfsutils). All documentation below is our own synthesis.

## 1. HFS+ volume layout (big picture)

An HFS+ volume is a flat sequence of allocation blocks. Byte 0–1023 are boot
blocks (outside the block space in practice). Everything multibyte is
big-endian.

```
offset 0:     boot blocks (1024 bytes, ignored)
offset 1024:  volume header (512 bytes)          <- signature 'H+' (0x482B)
...           allocation blocks (blockSize bytes each; block 0 starts at byte 1024)
end - 1024:   alternate volume header (backup copy)
```

Allocation block N lives at byte offset `1024 + N * blockSize`.

The volume header points at five special files via 80-byte "fork data"
descriptors (logical size, clump size, block count, 8 extent descriptors):

| Special file   | Role                                              |
|----------------|---------------------------------------------------|
| Allocation     | bitmap: 1 bit per allocation block                |
| Extents        | B-tree of overflow extents (> 8 extents per fork) |
| Catalog        | B-tree of every file/folder/thread record         |
| Attributes     | B-tree of extended attributes                     |
| Startup        | boot helper for non-Mac systems                    |

The **catalog file** is what DiskWarrior rebuilds. If its B-tree is damaged,
normal tools (fsck_hfs, which patches the existing tree in place) fail, but
the file/folder records often still exist as intact byte sequences scattered
in the catalog's former blocks or elsewhere on disk.

## 2. Volume header (512 bytes at offset 1024)

| Offset | Size | Field            | Notes                                    |
|--------|------|------------------|------------------------------------------|
| +0     | 2    | signature        | 0x482B 'H+', 0x4858 'HX' (HFSX)          |
| +2     | 2    | version          | 4 = HFS+, 5 = HFSX                       |
| +4     | 4    | attributes       | bit 8 (0x0100) = cleanly unmounted       |
| +8     | 4    | lastMountedVersion | OS signature that last mounted         |
| +12    | 4    | journalInfoBlock | 0 if no journal                          |
| +16    | 4    | createDate       | Mac time (**local** time, not GMT!)      |
| +20    | 4    | modifyDate       | Mac time (GMT)                           |
| +24    | 4    | backupDate       | Mac time (GMT)                           |
| +28    | 4    | checkedDate      | Mac time (GMT)                           |
| +32    | 4    | fileCount        |                                          |
| +36    | 4    | folderCount      |                                          |
| +40    | 4    | blockSize        | usually 4096                             |
| +44    | 4    | totalBlocks      |                                          |
| +48    | 4    | freeBlocks       |                                          |
| +52    | 4    | nextAllocation   | allocation hint                          |
| +56    | 4    | rsrcClumpSize    | typically 4 * blockSize                  |
| +60    | 4    | dataClumpSize    | typically 4 * blockSize                  |
| +64    | 4    | nextCatalogID    | next free CNID (>= 16)                   |
| +68    | 4    | writeCount       |                                          |
| +72    | 8    | encodingsBitmap  |                                          |
| +80    | 32   | finderInfo       | 8 x UInt32                               |
| +112   | 80   | allocationFile   | fork data                                |
| +192   | 80   | extentsFile      | fork data                                |
| +272   | 80   | catalogFile      | fork data  <- the one we care about      |
| +352   | 80   | attributesFile   | fork data                                |
| +432   | 80   | startupFile      | fork data                                |

Fork data (80 bytes): logicalSize u64, clumpSize u32, totalBlocks u32,
then 8 extent descriptors (startBlock u32, blockCount u32 each).

Reserved catalog node IDs (CNIDs): 1 = root parent, 2 = root folder,
3 = extents file, 4 = catalog file, 5 = bad blocks, 6 = allocation file,
7 = startup, 8 = attributes file. First user CNID = 16.

Mac absolute time = seconds since 1904-01-01 00:00:00 GMT.
Unix time = mac_time - 2082844800. Range ends 2040-02-06 (32-bit overflow).

## 3. B-tree nodes

Every B-tree (catalog, extents, attributes) is a set of fixed-size nodes
(`nodeSize` from the header record, usually 4096). Node 0 is always the
header node.

Node layout:

```
+------------------+  14 bytes: BTNodeDescriptor
| node descriptor  |
+------------------+
| record 0         |  key (2-byte length prefix) + record data
+------------------+
| record 1         |
+------------------+
| ...              |
+------------------+
| free space       |
+------------------+
| offset[N-1]      |  2 bytes each, packed at the very end of the node
| ...              |  offset[i] is at nodeSize - 2*(i+1), points at record i
| offset[0]        |
+------------------+
```

BTNodeDescriptor (14 bytes): fLink u32 (next node), bLink u32 (prev node),
kind s8 (-1 = leaf, 0 = index, 1 = header, 2 = map), height u8,
numRecords u16, reserved u16.

Node 0 (header node) body is a BTHeaderRec (106 bytes):

| Offset | Size | Field         |
|--------|------|---------------|
| +0     | 2    | treeDepth     |
| +2     | 4    | rootNode      |
| +6     | 4    | leafRecords   |
| +10    | 4    | firstLeafNode |
| +14    | 4    | lastLeafNode  |
| +18    | 2    | nodeSize      |
| +20    | 2    | maxKeyLength  |
| +22    | 4    | totalNodes    |
| +26    | 4    | freeNodes     |
| +30    | 2    | reserved1     |
| +32    | 4    | clumpSize     |
| +36    | 1    | btreeType     |  // 0 = catalog (HFS+), 255 = extents
| +37    | 1    | keyCompareType|  // 0xBC = case-insensitive (H+), 0xCF = binary (HX)
| +38    | 4    | attributes    |
| +42    | 64   | reserved3     |

Leaf nodes chain via fLink (0 = end). A healthy catalog is walked by
starting at firstLeafNode and following fLink.

## 4. Catalog records

Catalog key: keyLength u16, parentID u32 (CNID of parent folder),
then HFSUniStr255: length u16 + length*2 bytes UTF-16BE.
keyLength covers everything after itself: `6 + nameLen*2`.

Record types (first u16 of record data):

| Value  | Record              | Size        |
|--------|---------------------|-------------|
| 0x0001 | folder              | 54 bytes    |
| 0x0002 | file                | 214 bytes   |
| 0x0003 | folder thread       | up to 520   |
| 0x0004 | file thread         | up to 520   |

Folder record (54): recordType, flags u16, valence u32, folderID u32
(CNID), 5 Mac dates (create, contentMod, attributeMod, access, backup),
BSD info (16: ownerID, groupID, adminFlags u8, ownerFlags u8, fileMode u16,
special u32), textEncoding u16, reserved u32.

File record (214): recordType, flags u16, reserved1 u32, fileID u32 (CNID),
5 Mac dates, BSD info (16), textEncoding u16, reserved2 u32,
dataFork (80), resourceFork (80).

Thread record (8 + name): recordType, reserved u16, parentID u32 (the CNID
of the *file/folder this thread describes*'s parent), nodeName
HFSUniStr255. A thread key is (CNID, "") — empty name. Threads are how you
resolve a CNID back to (parent, name): the missing half of every record.
**Path resolution = follow thread records from a CNID up to root (2).**

A file's data lives where its dataFork extents say: each extent is
(startBlock, blockCount); bytes = blocks * blockSize, truncated to
logicalSize. More than 8 extents spill into the extents-overflow B-tree.

## 5. How DiskWarrior-style rebuilding works

When the catalog B-tree is corrupt, fsck fails because it tries to repair
the tree structure in place. The rebuild approach instead:

1. **Find candidate catalog data.** Read the volume header's catalogFile
   fork data -> extent list -> raw blocks. If the header itself is damaged,
   fall back to the alternate header (volume_size - 1024), or scan the
   whole disk for blocks that look like B-tree nodes (see below).
2. **Harvest records.** Walk every plausible leaf node and pull out every
   record whose type field is 1–4 and whose key parses cleanly. Corrupt
   nodes are skipped; partial harvest still beats nothing.
3. **Rebuild the namespace.** Index thread records by CNID. For every file
   and folder record, resolve its full path by chasing thread records to
   the root. Records with no thread (orphans) are grouped by parentID
   heuristics or placed in a "recovered orphans" folder — exactly what
   DiskWarrior's "recovered files" output is.
4. **Rebuild extents.** For each file, take the 8 inline extents; if
   logicalSize isn't covered, consult the extents-overflow B-tree the
   same harvest way.
5. **Write a new catalog** (or just extract the files). Writing is the
   dangerous half; extraction-only is the safe R&D target.

**Node-signature scan** (the key primitive for damaged volumes): a
plausible B-tree node descriptor at a nodeSize-aligned offset satisfies:
kind in {-1, 0, 1, 2}, numRecords small enough that
`14 + numRecords*min_record + 2*numRecords <= nodeSize`, and every
record offset in the trailing array points inside the node, is >= 14,
and offsets are strictly increasing. False positives are possible;
each candidate is validated by trying to parse its records.

## 6. What exists here

- `hfs_parse.py` — read-only parsers: volume header, fork data, B-tree
  node descriptor, B-tree header record, catalog keys, folder / file /
  thread records, BSD info, Mac timestamps. Plus `HFSVolume`, a small
  class that opens an image read-only, walks the catalog B-tree, resolves
  paths via thread records, and extracts file data via extents.
- `scan_nodes.py` — node-signature scanner: sweeps raw bytes for plausible
  B-tree node descriptors (the DiskWarrior-style harvest primitive).
- `build_test_image.py` — builds a tiny synthetic HFS+ image (volume
  header + 2-node catalog + one file) so the parsers can be tested without
  a real Mac volume.
- `test_hfs.py` — end-to-end test: build image, parse header, walk
  catalog, resolve `/Docs/hello.txt`, extract its bytes, and verify the
  node scanner finds both B-tree nodes.

## 7. Roadmap / feasibility notes

Done: header parse, B-tree walk of a healthy catalog, record parsing,
path resolution, file extraction, node-signature scanning.

Hard parts ahead (ranked):
1. **Real-world catalog parsing robustness** — production catalogs have
   millions of records, index nodes, and edge cases (hard links via
   hidden files, decmpfs-compressed files). Our walker handles leaf
   chains; index-node descent for targeted lookup is next.
2. **Journal replay** — journaled volumes (most modern ones) may have
   catalog updates only in the journal. Reading the journal is a project
   of its own; without it, a rebuilt catalog can miss recent files.
3. **Harvest quality on truly damaged media** — distinguishing a damaged
   leaf node from random data, and reassembling records split across
   node boundaries, is heuristic work validated only against real
   damaged images.
4. **Writing a new catalog** — the actual "repair" half. High risk;
   extraction-first is the sane deliverable.

Estimate: healthy-volume parsing/extraction is ~80% done here. A
DiskWarrior-class rebuild (harvest + orphan reassembly + new-catalog
write, validated on real damaged volumes) is a multi-week project, most
of it in testing against real damaged images, not in parsing code.

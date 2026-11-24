# HFS+ Parser Gaps — Pre-Flight Checklist for CreytixVault2TB

Research synthesis: edge cases in HFS+ that our parser (`hfs_parse.py`)
does not handle yet, ranked by impact on recovering the CreytixVault2TB
(Journaled HFS+, catalog file damaged, fsck/First Aid failed with exit 5).

Reference: Apple's TN1150 (https://preterhuman.net/macstuff/technotes/tn/tn1150.html),
verified directly for struct layouts below.

## Status update — 2026-11-23 (G5/G6/G7 implemented, re-built after VM clear)

- **G5 ✅** `detect_crosslinks(volume)` in `hfs_parse.py` (+ `--crosslinks` CLI flag).
- **G6 ✅ (assessment only)** `check_journal(volume_data, vol)` — read-only
  journal assessment, NO replay. Full replay remains a separate sub-project.
- **G7 ✅** `try_alternate_header(volume_data, total_size)`; `HFSVolume`
  falls back to the alternate header on primary `ValueError`
  (`vol.header_source` is `"primary"` or `"alternate"`).
- Tests: `test_hfs.py` 66 PASS / 0 FAIL (incl. 35 new G5/G6/G7 checks);
  `test_damage.py` still passes (6 PASS).
- Still open: G8–G12 (P2), G13–G16 (P3) — see below.

---

## P0 — CRITICAL: wrong data extracted (fix before any real media)

### G1. File/folder records missing 32 bytes of Finder info

**What:** TN1150 defines `HFSPlusCatalogFile` as 248 bytes, including
`FInfo userInfo` (16 bytes) and `FXInfo finderInfo` (16 bytes) between
`permissions` and `textEncoding`. Our `parse_catalog_file_record` assumes
214 bytes and reads `textEncoding`/`dataFork`/`resourceFork` 32 bytes too
early. Same for `HFSPlusCatalogFolder`: real size is 88 bytes (includes
`DInfo` + `DXInfo`), we assume 54.

**Impact:** Every data-fork extent descriptor is read from the wrong offset.
`read_fork()` will read **wrong allocation blocks** — extracting garbage or
crashing on out-of-range blocks. The synthetic test passed only because
`build_test_image.py` used the same wrong layout.

**Fix:** Insert 32 bytes (userInfo + finderInfo) after `permissions` in both
record parsers. Correct offsets:
- File: recordType(0) flags(2) reserved1(4) fileID(8) dates(12..32)
  permissions(32..48) userInfo(48..64) finderInfo(64..80)
  textEncoding(80) reserved2(84) dataFork(88..168) resourceFork(168..248)
- Folder: recordType(0) flags(2) valence(4) folderID(8) dates(12..32)
  permissions(32..48) userInfo(48..64) finderInfo(64..80)
  textEncoding(80) reserved(84); total 88 bytes.

**Bonus:** Parsing `userInfo`/`finderInfo` also gives us the Finder type/
creator codes needed for hard-link detection (G4).

**Ref:** TN1150 "Catalog File" section, `struct HFSPlusCatalogFile`,
`struct HFSPlusCatalogFolder`.

### G2. Extents overflow B-tree not consulted

**What:** Catalog file records hold only the first 8 extents per fork.
Forks with >8 extents spill into the extents-overflow B-tree (keyed by
`HFSPlusExtentKey`: forkType, fileID, startBlock). Our `read_fork()` stops
after the 8 inline extents.

**Impact:** Any fragmented file with >8 extents is silently truncated.
On a 2TB data volume, large files (videos, disk images, archives) routinely
exceed 8 extents. We would extract partial files with no warning.

**Fix:** After inline extents, check if `logical_size` is covered. If not,
walk the extents-overflow B-tree (via the volume header's `extents_file`
fork) for keys matching (fileID, forkType), chaining by `startBlock`.

**Ref:** TN1150 "Extents Overflow File" section; `struct HFSPlusExtentKey`
(keyLength=10: forkType u8, pad u8, fileID u32, startBlock u32).

---

## P1 — HIGH: data correctness on real volumes

### G3. Transparent compression (decmpfs) not detected

**What:** macOS 10.6+ compresses files transparently. The compression type
lives in the extended attribute `com.apple.decmpfs`. Depending on type,
the data fork contains a small header (not file data) and the real bytes
are in the resource fork or stored inline compressed. Types include raw
zlib, LZVN, and LZFSE (types 7, 8, 11, 12 use Apple's LZFSE reference
codecs).

**Impact:** Extracting a decmpfs file's data fork raw yields a ~4KB header,
not the file content. Common on any HFS+ volume from the last 15 years,
including user documents.

**Fix:** Check `kHFSHasAttributesMask` (0x0004) flag → read attributes
B-tree for `com.apple.decmpfs` → decompress accordingly. At minimum,
detect and flag compressed files rather than silently extracting garbage.

**Ref:** Apple `decmpfs.h` (xnu source); LZFSE reference at
https://github.com/lzfse/lzfse.

### G4. File hard links not resolved

**What:** A hard link is a file record whose `flags` has
`kHFSHasLinkChainMask` (0x0020) and whose Finder type/creator is
`'hlnk'`/`'hfs+'`. The record's own data fork is empty; the real data is
in the indirect node file `iNode<NNN>` inside the hidden
`\0\0\0\0HFS+ Private Data` directory, where NNN comes from
`permissions.special` (the `bsd_info["special"]` field we already parse).

**Impact:** We would extract 0-byte files for every hard link. (Less
likely on CreytixVault2TB than on a Time Machine volume, but must handle.)

**Fix:** When `flags & 0x0020` and type/creator match, resolve via the
indirect node: look up the `iNode<NNN>` file record, use *its* data fork
extents and size. Requires parsing `userInfo` (G1 bonus).

**Ref:** TN1150 "Hard Links" section; observed behavior documented at
https://chridd.nfshost.com/hfsplus/ and
https://github.com/hawkynt/compressionworkbench (commit 03530e8).

### G5. Cross-linked extents not detected

**What:** Corruption can cause two file records to claim the same
allocation blocks. DiskWarrior detects and reports these; the operator
decides which file owns the blocks.

**Impact:** We would silently extract both files with overlapping/garbled
content and report success.

**Fix:** After harvesting all file records, build an interval map of
(start_block, block_count) → [CNIDs]. Flag any block claimed by >1 file
(excluding legitimate hard-link sharing, G4). Report in the recovery
summary; optionally extract only the first claimant.

### G6. Journal not replayed

**What:** CreytixVault2TB is journaled (fsck reported "Checking Journaled
HFS Plus volume"). The volume header's `journal_info_block` field points
to the allocation block containing the `JournalInfoBlock`, which locates
the journal header + circular buffer. If the journal is non-empty
(unclean unmount), the newest catalog updates may exist *only* in the
journal — the on-disk catalog is stale.

**Impact:** Rebuilt catalog may miss recently created/modified files.

**Fix (sub-project):** Parse `JournalInfoBlock` → journal header
(start/end offsets) → iterate transactions → for each block-list,
write blocks to their on-disk locations (in memory, on our image copy —
never the original). TN1150 documents the full journal format including
the circular-buffer wrap rule.

**Ref:** TN1150 "Journal" section (Figures 8–9, `struct JournalInfoBlock`,
journal header layout).

---

## P2 — MEDIUM: completeness and robustness

### G7. No alternate volume header fallback

**What:** We read only the primary header at byte 1024. A backup copy
lives at (volume_size − 1024). If the primary is damaged, the alternate
may be intact.

**Fix:** On `ValueError` from primary parse, try the alternate. Volume
size must come from the partition table or device size.

**Ref:** TN1150 "Volume Header" section.

### G8. POSIX filename mapping not applied

**What:** HFS+ stores names in Finder form; POSIX swaps `/` and `:`.
A Finder name `a:b` is stored as `a/b` in the catalog key. Our
`decode_hfs_unistr` returns raw catalog bytes.

**Impact:** Extracted files get wrong names; a `/` in a catalog name
(which displays as `:` in Finder) would create phantom subdirectories
on extraction to a POSIX filesystem.

**Fix:** When materializing paths for extraction, swap `/` ↔ `:` per
Apple's mapping. (Also: U+0000 displays as U+2400.)

**Ref:** Observed/fixed in https://github.com/hawkynt/compressionworkbench
(commit 03530e8): "The catalog stores names as the Finder shows them;
the POSIX view swaps '/' and ':'".

### G9. Directory hard links (Time Machine) not resolved

**What:** Directory hard links (Time Machine) use a stub file record with
Finder type `'fdrp'`, creator `'MACS'`, **and** flag `kHFSHasLinkChainMask`.
Real data lives in `.HFS+ Private Directory Data\r/dir_<inode>`. Warning:
plain Finder aliases share the `fdrp`/`MACS` pair but lack the link-chain
flag — must check the flag to distinguish.

**Impact:** On a Time Machine volume, we would list link stubs as empty
files and miss entire directory trees. Probably not present on
CreytixVault2TB (data volume, not backup), but the harvester should at
least flag `fdrp`/`MACS` records.

**Fix:** Detect the triple (type/creator/flag) → resolve `dir_<inode>`
under the private directory → treat as the linked folder's contents.

**Ref:** https://github.com/aoiflux/libhfs (commit c2921c1, dir-link
resolution); https://www.forensicfocus.com/forums/general/hfs-private-directory-data-dir/

### G10. Resource fork not extracted by CLI

**What:** We parse the resource fork descriptor but `--extract` only
writes the data fork. Classic Mac files and some modern files (custom
icons, decmpfs-compressed data) keep meaningful data in the resource
fork.

**Fix:** Add `--extract-rsrc` or write `<name>.rsrc` alongside.

### G11. Unicode normalization (NFD) not applied for lookup

**What:** HFS+ stores names in Apple's NFD variant (decomposed). Our
decoder returns NFD strings as-is, which is correct for extraction to a
non-normalizing filesystem (Linux). But any *search-by-name* feature
must normalize the query to NFD first, or `café` (NFC) won't match.

**Fix:** `unicodedata.normalize('NFD', query)` before comparison. Note
Apple's variant differs from standard NFD for a handful of characters;
edge-case only.

**Ref:** https://ss64.com/mac/syntax-filenames.html

### G12. Allocation bitmap not cross-checked

**What:** We don't verify that a file's extents point at allocated blocks
(via the allocation file bitmap). On damaged media, extents may reference
unallocated (garbage) blocks.

**Fix:** Parse the allocation bitmap; flag files whose extents touch
unallocated ranges as suspect in the recovery report.

---

## P3 — LOW / future work

### G13. Extended attributes and ACLs ignored
Flags `kHFSHasAttributesMask` (0x0004) / `kHFSHasSecurityMask` (0x0008)
indicate attributes-B-tree records. Mostly non-critical for data recovery
(quarantine flags, Finder labels, ACLs) — except `com.apple.decmpfs`
(G3), which is P1.

### G14. No index-node descent
We walk leaves via `fLink` only. Fine for harvesting (we want everything),
but a broken `fLink` mid-chain silently drops all subsequent leaves.
`scan_nodes.py` (sector-signature sweep) is the robust path and doesn't
depend on `fLink` — prefer it for damaged volumes.

### G15. HFSX case sensitivity
Header version 5 / signature `'HX'` with `keyCompareType 0xCF` means
case-sensitive (binary compare) vs `0xBC` (case-folding). Irrelevant for
harvest-and-extract; matters only if we build a lookup index later.

### G16. Key-length validation against maxKeyLength
We bound keys within the node (`key_end <= rec_end`), which prevents
overruns. Apple's CVE-2026-43682 (HFS+ B-tree kernel heap overflow) came
from trusting key length against `maxKeyLength` — our bound check covers
the practical risk for a read-only parser, but an explicit
`key_len <= maxKeyLength` assert would be defense in depth.
Ref: https://github.com/petermalone/CVE-2026-43682

---

## DiskWarrior technique summary (from public sources)

What DiskWarrior is documented to do (Alsoft manual, Macworld, patent
descriptions) — mapped to our implementation status:

| Technique | Description | Our status |
|-----------|-------------|------------|
| Scavenge | Read every sector; collect directory fragments, not just the B-tree | ✅ `scan_nodes.py` |
| Rebuild, don't patch | Build a *new* catalog from harvested records; never modify the original | ✅ harvest-only design |
| Preview before write | Mount/show the rebuilt tree before committing | ✅ extraction-first (no writes at all) |
| Orphan grouping | Files with no resolvable parent go to a "recovered files" folder | ⚠️ we emit `<orphan:CNID>` paths; grouping is cosmetic |
| Cross-link detection | Flag blocks claimed by >1 file | ✅ G5 (`detect_crosslinks`) |
| Damaged-node skipping | Skip corrupt nodes, keep harvesting the rest | ✅ try/except in `iter_catalog_records` |
| Scavenge mode | Hold Option to scan even healthy regions | ✅ our harvester always scans everything |

## Open-source tool landscape (for reference / fallback)

| Tool | Approach | Limitations |
|------|----------|-------------|
| PhotoRec/TestDisk (CGSecurity, GPL) | Signature-based carving; ignores filesystem | No filenames, no folder structure; last resort |
| `chridd` hfsplus (C, as-is) | HFS+ reader | Author notes dir-hard-link behavior undocumented; lightly tested |
| `aoiflux/libhfs` (Go) | HFS+ reader, handles dir links | Reader only, no recovery |
| `hawkynt/compressionworkbench` (C#) | HFS+ read/write w/ hard-link + POSIX-name fixes | General-purpose, not recovery-focused |
| DFSee | Commercial-ish disk tool, HFS+ catalog display | Not a rebuilder |
| R-Studio / UFS Explorer | Commercial recovery suites | Good HFS+, but closed source; ~$80 |
| `randynorthrup/s.a.k.-utility` | HFS+ partition manager, B-tree surgery | Write-oriented; useful reference for B-tree split/insert mechanics |

## Priority fix order for CreytixVault2TB

1. **G1** (record layout) — everything downstream is wrong until this is fixed.
2. **G2** (extents overflow) — large files truncated otherwise.
3. **G7** (alternate header) — cheap insurance if primary header is shaky.
4. **G4** (file hard links) — cheap to detect; prevents 0-byte extractions.
5. **G3** (decmpfs) — detect-and-flag minimum; full decompression later.
6. **G6** (journal) — assess journal non-empty before deciding to invest.
7. **G5** (cross-links) — integrity reporting.
8. P2 items as encountered on the real volume.

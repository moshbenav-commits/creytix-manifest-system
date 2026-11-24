# Lessons Learned — Manifest System & HFS+ Recovery

Date: 2026-11-24. Source: the manifest-system / DiskWarrior R&D session (Oct–Nov 2026).

## Storage & manifests

1. **`/tmp` gets cleared — never put manifests there.** All scan outputs go in `runs/` (or another persistent directory). The 2TB Porsche manifest (3.26M lines, 944MB) was lost when Pro `/tmp` was cleared, forcing a full re-scan.
2. **Back up the database before re-init.** The 9.3M-file database was lost when the DB was re-initialized without a backup. Always `cp manifests.db manifests.db.bak.<date>` before any schema change or re-init.
3. **Scans must be resumable AND their outputs must survive reboots.** `manifest-agent.py --resume` handles interruption, but only if the output file itself survives. Outputs live in `runs/`, never `/tmp`.
4. **Verify files exist before reporting duplicates.** The WhatsApp `Video.tar` files appeared in the duplicate report (12 entries, 29 GB) but were gone from every machine when checked — the DB entries were stale from October scans. Always re-verify existence before any deletion.
5. **iCloud files: deleting from one Mac can propagate.** Files in `~/Library/Mobile Documents/` are iCloud-synced. Deleting from one machine can delete from iCloud and all other Macs. Check sync state and account before deleting; protect the keeper copy outside iCloud first.

## Fleet & networking

6. **Fleet SSH proxy drops — background jobs survive, transfers fail.** `scp` through the egress proxy fails with "Broken pipe" during outages. Use `ssh <host> "cat > /tmp/x" < file` (stdin transfer) instead of scp. Background `nohup` jobs on the Pro are unaffected by proxy drops.
7. **VM clears wipe uncommitted work — commit early, commit often.** The HFS+ G5–G7 code was lost in a VM clear and had to be re-implemented. The MacCare package was also lost once. If it is not in a repo, it does not exist.

## HFS+ recovery

8. **The catalog header is the single point of failure.** On the CreytixVault, the catalog B-tree header node was unreadable (`bad node_size 65535`), which is why `fsck_hfs` and Disk Utility both failed. The DiskWarrior approach — ignore the header, scan raw bytes for surviving leaf nodes — is the correct recovery path.
9. **Validate record layouts against real volumes, not just synthetic images.** The G1 bug (wrong 214-byte file record vs real 248-byte) passed all synthetic tests because the test image builder shared the same wrong layout. Always validate against a real Apple-produced volume.
10. **Read-only first, always.** Every tool in `hfs-recovery/` is read-only by design. Never run repair on the original — image first, recover from the image.

## Process

11. **One command, one pipeline.** `pipeline.py full` (scan → collect → ingest → report) replaced a chain of manual steps that each had a failure mode. Automate the chain; do not rely on remembering the steps.
12. **Every drive gets a scan before any deletion.** The "keep" list comes from the manifest, not from memory. No wipe, reformat, or delete happens until the drive is inventoried and the user approves the keep list.
13. **Stale data is worse than no data.** A database that says a file exists when it does not leads to wasted work and wrong decisions. Re-scan on a schedule (weekly) to keep the ledger current.

## Deletion-batch lessons (2026-11-24)

14. **Purge deleted rows from the DB after every deletion batch.** The DB goes stale and future duplicate queries surface ghosts. Batches 1+2 deleted 25 files (~299 GB); 23 rows had to be purged afterward. WhatsApp `Video.tar` and Chrome `weights.bin` entries were still listed after the files were deleted.
15. **HuggingFace cache snapshot files are symlinks to blobs — check inodes before counting duplicates.** A snapshot+blob pair is ONE real copy, not two. The ACE-Step model had only 1 real 6.6 GB copy on the Vault; the Studio copies were already offloaded away and the Vault "second" copy was a symlink to the first.
16. **Never delete from inside a Photos Library (.photoslibrary).** Same-hash files in `Masters/` are library-managed; deleting corrupts the library. `IMG_1765.MOV` (10.7 GB x 2) was left alone.
17. **Same-hash files on different machines are not duplicates.** Each machine legitimately needs its own copy. `qwen3.8-27b.gguf` (16.8 GB on Pro + Studio) was left alone.
18. **Temp files clean themselves up — verify existence on the RIGHT machine before presenting a batch.** Apple Configurator `TemporaryItems` were gone from mini32 by the time we checked. The first existence check ran Pro paths for mini32 files and falsely reported them missing.

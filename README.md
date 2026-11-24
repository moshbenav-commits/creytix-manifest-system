# creytix-manifest-system

Cross-drive file inventory, dedupe ledger, and HFS+ catalog-recovery R&D.
The "brain" for organizing files across many drives, machines, and clouds.

## What it is

Three tools that work together:

| Directory | What it does |
|---|---|
| `manifest-system/` | Scan drives/machines/clouds, build a unified SQLite ledger, find duplicates by hash, keep the ledger clean. The pipeline orchestrator (`pipeline.py`) runs scan → collect → ingest → report → clean across a fleet. |
| `hfs-recovery/` | Reverse-engineering the core of DiskWarrior: rebuild a damaged HFS+ catalog from raw disk bytes. Read-only — never writes to a disk device. |
| `maccare/manifest-pipeline/` | The MacCare commercial package: a standalone, stdlib-only version of the manifest pipeline that runs on any Mac with no dependencies. |

## Quickstart

```bash
# Scan a drive (resumable, read-only)
python3 manifest-system/manifest-agent.py /Volumes/MyDrive \
  --output mydrive.manifest.jsonl --source "MyDrive"

# Ingest into the ledger
python3 manifest-system/manifest-db.py ingest mydrive.manifest.jsonl

# Find duplicates across everything scanned
python3 manifest-system/manifest-db.py duplicates

# Stats
python3 manifest-system/manifest-db.py stats
```

For the full pipeline (fleet scans + collect + ingest + report), see
`manifest-system/README.md` and `pipeline-config.yaml`.

## The story

Built October–November 2026 while consolidating a fleet of Macs and external
drives (2TB Porsche, 5TB Sabrent, 8TB Seagate, Macintosh HD 1/2, plus
mini32/Air/Studio/mini8/minitool). The ledger grew to 11M+ files across
4+ TB. The HFS+ work started when a 2TB LaCie (`CreytixVault2TB`) refused
to mount — its catalog was corrupt, and rather than pay for DiskWarrior we
rebuilt the recovery sequence from Apple's TN1150 spec. All seven gaps in
the gap analysis (G1–G7) are closed and tested; the parser is ready for a
read-only trial on a damaged volume.

The old 2TB vault's contents were never recovered — the drive was erased
and reformatted in October 2026 before the parser was ready. The 5TB
`CreytixVault` volume is different content, not a copy.

## Rules

- **Nothing is deleted until verified.** A duplicate is only a candidate
  until a byte-identical hash is confirmed AND a verified backup exists
  elsewhere.
- **Read-only by design.** The scanner and the HFS+ parser never modify
  source files or disk devices.
- **Manifests are the source of truth.** Scan outputs go to a permanent
  folder, never `/tmp` (it gets cleared).

## License

MIT. See `LICENSE`. All code in this repo is original work, Python standard
library only (Pillow is an optional, unbundled dependency for EXIF dates).

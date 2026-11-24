# Manifest System

The brain for Giulana's data organization project. Scans drives/machines/clouds,
builds a unified manifest database, finds duplicates, and plans intelligent placement.

## Components

### 1. manifest-agent.py
Scans a directory/drive, produces a JSONL manifest with:
- Path, size, modification time
- SHA256 hash (for dedupe)
- File type (image/video/document/other)
- EXIF date (for images, if PIL available)

```bash
# Full scan with hashing
python3 manifest-agent.py /Volumes/2TB --output /tmp/2tb.manifest.jsonl --source "2TB-Porsche"

# Fast metadata-only scan (no hashing)
python3 manifest-agent.py /Volumes/2TB --output /tmp/2tb.manifest.jsonl --source "2TB-Porsche" --no-hash

# Resume interrupted scan
python3 manifest-agent.py /Volumes/2TB --output /tmp/2tb.manifest.jsonl --source "2TB-Porsche" --resume
```

### 2. manifest-db.py
SQLite database for all manifests. Cross-drive duplicate detection.

```bash
python3 manifest-db.py init
python3 manifest-db.py ingest /tmp/2tb.manifest.jsonl
python3 manifest-db.py ingest /tmp/gdrive.manifest.jsonl
python3 manifest-db.py duplicates    # find all duplicates by hash
python3 manifest-db.py stats        # summary
python3 manifest-db.py find <sha256>  # find all copies of a file
```

## Workflow

1. **Scan**: Run manifest-agent on each drive/machine/cloud
2. **Ingest**: Load all manifests into the central DB
3. **Analyze**: Run duplicates, stats to understand the data
4. **Plan**: (future) Generate placement/migration plans
5. **Execute**: (future) Safe move/delete with verification

## Design Principles

- **Manifests are portable**: JSONL files can be generated on any machine and shipped back
- **Resumable**: Interrupted scans pick up where they left off
- **Hash-verified**: Every dedupe decision is backed by SHA256
- **Read-only scanning**: The agent never modifies source files
- **Compatible**: Works alongside organizer-toolkit (the "hands")

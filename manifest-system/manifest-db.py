#!/usr/bin/env python3
"""
Manifest DB — SQLite database for storing and querying file manifests.

Ingests JSONL manifests from manifest-agent.py, enables cross-drive
duplicate detection, and generates reports.

Usage:
  python3 manifest-db.py init                    # create database
  python3 manifest-db.py ingest /tmp/2tb.manifest.jsonl
  python3 manifest-db.py duplicates               # find duplicates by hash
  python3 manifest-db.py stats                    # summary statistics
  python3 manifest-db.py find <sha256>            # find all copies of a file
"""

import os
import sys
import json
import sqlite3
import argparse
from collections import defaultdict

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manifests.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    size INTEGER,
    mtime TEXT,
    type TEXT,
    sha256 TEXT,
    md5 TEXT,
    exif_date TEXT,
    UNIQUE(path, source)
);
CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_md5 ON files(md5);
CREATE INDEX IF NOT EXISTS idx_source ON files(source);
CREATE INDEX IF NOT EXISTS idx_type ON files(type);
CREATE INDEX IF NOT EXISTS idx_size ON files(size);
"""

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def cmd_init(args):
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def cmd_ingest(args):
    conn = get_db()
    conn.executescript(SCHEMA)

    count = 0
    with open(args.manifest, 'r') as f:
        for line in f:
            try:
                e = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            conn.execute("""
                INSERT OR REPLACE INTO files (path, source, size, mtime, type, sha256, md5, exif_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e.get('path'), e.get('source'), e.get('size'),
                e.get('mtime'), e.get('type'), e.get('sha256'),
                e.get('md5'), e.get('exif_date'),
            ))
            count += 1
            if count % 1000 == 0:
                conn.commit()
    conn.commit()
    conn.close()
    print(f"Ingested {count} entries from {args.manifest}")

def cmd_duplicates(args):
    """Find files with the same hash on different paths/sources."""
    conn = get_db()
    cur = conn.execute("""
        SELECT sha256, COUNT(*) as n, SUM(size) as total_size
        FROM files
        WHERE sha256 IS NOT NULL
        GROUP BY sha256
        HAVING n > 1
        ORDER BY total_size DESC
    """)
    dupes = cur.fetchall()

    total_wasted = 0
    print(f"\nFound {len(dupes)} duplicate groups:\n")
    for d in dupes[:50]:  # top 50 by size
        sha = d['sha256']
        n = d['n']
        # Get all paths for this hash
        paths = conn.execute(
            "SELECT path, source, size FROM files WHERE sha256 = ?", (sha,)
        ).fetchall()
        wasted = paths[0]['size'] * (n - 1)
        total_wasted += wasted
        print(f"  {n}x copies, {paths[0]['size']:,} bytes each (wastes {wasted:,} bytes)")
        for p in paths[:5]:
            print(f"    - [{p['source']}] {p['path']}")
        if len(paths) > 5:
            print(f"    ... +{len(paths)-5} more")
        print()

    print(f"Total reclaimable from duplicates: {total_wasted:,} bytes ({total_wasted/1e9:.1f} GB)")
    conn.close()

def cmd_stats(args):
    conn = get_db()

    total = conn.execute("SELECT COUNT(*), SUM(size) FROM files").fetchone()
    by_type = conn.execute(
        "SELECT type, COUNT(*), SUM(size) FROM files GROUP BY type"
    ).fetchall()
    by_source = conn.execute(
        "SELECT source, COUNT(*), SUM(size) FROM files GROUP BY source"
    ).fetchall()
    no_hash = conn.execute(
        "SELECT COUNT(*) FROM files WHERE sha256 IS NULL"
    ).fetchone()[0]

    print(f"\nTotal files: {total[0]:,}")
    print(f"Total size: {total[1]:,} bytes ({total[1]/1e12:.2f} TB)")
    print(f"Files without hash: {no_hash:,}")
    print("\nBy type:")
    for t in by_type:
        print(f"  {t[0] or 'unknown'}: {t[1]:,} files, {t[2]:,} bytes")
    print("\nBy source:")
    for s in by_source:
        print(f"  {s[0]}: {s[1]:,} files, {s[2]:,} bytes")
    conn.close()

def cmd_find(args):
    conn = get_db()
    rows = conn.execute(
        "SELECT path, source, size, mtime FROM files WHERE sha256 = ?", (args.sha256,)
    ).fetchall()
    if not rows:
        print("No matches found.")
    else:
        print(f"\nFound {len(rows)} copies:")
        for r in rows:
            print(f"  [{r['source']}] {r['path']} ({r['size']:,} bytes, {r['mtime']})")
    conn.close()

def main():
    parser = argparse.ArgumentParser(description='Manifest DB')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('init', help='Initialize database')
    p_ingest = sub.add_parser('ingest', help='Ingest a manifest JSONL file')
    p_ingest.add_argument('manifest', help='Path to .jsonl manifest file')
    sub.add_parser('duplicates', help='Find duplicate files by hash')
    sub.add_parser('stats', help='Show summary statistics')
    p_find = sub.add_parser('find', help='Find all copies of a file by hash')
    p_find.add_argument('sha256', help='SHA256 hash to search for')

    args = parser.parse_args()
    if args.command == 'init':
        cmd_init(args)
    elif args.command == 'ingest':
        cmd_ingest(args)
    elif args.command == 'duplicates':
        cmd_duplicates(args)
    elif args.command == 'stats':
        cmd_stats(args)
    elif args.command == 'find':
        cmd_find(args)

if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Google Drive Manifest Agent — scans Google Drive and produces manifests.

Lists all files in Drive, captures metadata (name, size, modified time,
MD5 checksum), and outputs JSONL compatible with manifest-db.py.

Note: Drive provides MD5 (not SHA256). The manifest-db can match on MD5
for Drive files, or you can download and re-hash for SHA256.

Usage:
  python3 gdrive-manifest.py --output /tmp/gdrive.manifest.jsonl
  python3 gdrive-manifest.py --output /tmp/gdrive.manifest.jsonl --max 2000
"""

import os
import sys
import json
import argparse
import subprocess
from datetime import datetime

GWS = "/opt/hatch/bin/hatch_gws_cli"

def run_gws(*args):
    """Run hatch_gws_cli and return parsed output."""
    cmd = [GWS] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"GWS error: {result.stderr[:300]}", file=sys.stderr)
            return None
        # Try JSON, fall back to line-delimited JSON
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            items = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return items
    except subprocess.TimeoutExpired:
        print("GWS timed out", file=sys.stderr)
        return None
    except Exception as ex:
        print(f"GWS failed: {ex}", file=sys.stderr)
        return None

def list_all_files(max_results=5000):
    """List all non-trashed files in Drive with pagination."""
    all_files = []
    page_token = None

    while len(all_files) < max_results:
        params = {
            "pageSize": 1000,
            "q": "trashed=false",
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, md5Checksum, parents)",
        }
        if page_token:
            params["pageToken"] = page_token

        result = run_gws("drive", "files", "list", "--params", json.dumps(params))
        if not result:
            break

        if isinstance(result, dict):
            files = result.get('files', [])
            all_files.extend(files)
            page_token = result.get('nextPageToken')
            if not page_token:
                break
        elif isinstance(result, list):
            # Line-delimited, take first page
            for item in result:
                if isinstance(item, dict) and 'files' in item:
                    all_files.extend(item['files'])
            break
        else:
            break

        print(f"  ... {len(all_files)} files listed", file=sys.stderr)

    return all_files[:max_results]

def drive_type(mime):
    """Map Drive MIME type to our type system."""
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    if mime in ('application/pdf', 'application/msword',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/vnd.google-apps.document'):
        return 'document'
    if 'folder' in mime:
        return 'folder'
    return 'other'

def main():
    parser = argparse.ArgumentParser(description='Google Drive Manifest Agent')
    parser.add_argument('--output', '-o', required=True, help='Output manifest JSONL')
    parser.add_argument('--max', type=int, default=5000, help='Max files (default: 5000)')
    parser.add_argument('--source', '-s', default='gdrive', help='Source label')
    args = parser.parse_args()

    print("Listing Google Drive files...", file=sys.stderr)
    files = list_all_files(args.max)
    print(f"Found {len(files)} files", file=sys.stderr)

    count = 0
    with open(args.output, 'w') as out:
        for f in files:
            # Skip folders (they have no content to hash)
            if 'folder' in f.get('mimeType', ''):
                continue

            entry = {
                'path': f"gdrive:{f.get('id')}:{f.get('name')}",
                'source': args.source,
                'size': int(f.get('size', 0)),
                'mtime': f.get('modifiedTime', ''),
                'type': drive_type(f.get('mimeType', '')),
                # Drive gives MD5, not SHA256. Store as md5 field.
                'md5': f.get('md5Checksum'),
                'sha256': None,  # Would need download to compute
                'drive_id': f.get('id'),
                'drive_name': f.get('name'),
                'mime': f.get('mimeType'),
            }
            out.write(json.dumps(entry) + '\n')
            count += 1

    print(f"Done: {count} files in manifest", file=sys.stderr)
    print("\nNote: Drive provides MD5 checksums. For SHA256 matching,", file=sys.stderr)
    print("download files and re-hash, or match on MD5 in the DB.", file=sys.stderr)

if __name__ == '__main__':
    main()

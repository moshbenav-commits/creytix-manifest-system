#!/usr/bin/env python3
# MacCare manifest-pipeline — packaged copy.
# Original work by Ricardo Benavente, developed for the manifest-system
# project (source: /Users/ricardo/workspace/manifest-system/).
# Packaged under the MIT License for the MacCare commercial toolkit.
# See LICENSE and THIRD-PARTY-NOTICES.md in the MacCare kit.
# No third-party code is included: Python standard library only
# (Pillow is an OPTIONAL, unbundled dependency for EXIF dates).
"""
Manifest Agent — scans a directory/drive and produces a standardized JSON manifest.

Each file gets: path, size, mtime, SHA256 hash, file type, and (for images)
EXIF date. Output is JSON Lines (one object per file), resumable via --resume.

Usage:
  python3 manifest-agent.py /Volumes/2TB --output /tmp/2tb.manifest.jsonl
  python3 manifest-agent.py /Volumes/2TB --output /tmp/2tb.manifest.jsonl --resume
"""

import os
import sys
import json
import hashlib
import argparse
import mimetypes
from datetime import datetime

# Try to import EXIF support (optional)
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

CHUNK_SIZE = 8192 * 1024  # 8MB chunks for hashing

def sha256_file(path):
    """Compute SHA256 hash of a file, reading in chunks."""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (IOError, OSError):
        return None

def get_exif_date(path):
    """Extract EXIF DateTimeOriginal from an image, if available."""
    if not HAS_PIL:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag = TAGS.get(tag_id, tag_id)
                    if tag == 'DateTimeOriginal':
                        return str(value)
    except Exception:
        pass
    return None

def is_image(path):
    """Check if file is an image by extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
                   '.webp', '.heic', '.heif', '.raw', '.cr2', '.nef', '.arw')

def is_video(path):
    """Check if file is a video by extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in ('.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm',
                   '.m4v', '.mpg', '.mpeg', '.3gp')

def is_document(path):
    """Check if file is a document by extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in ('.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.xls',
                   '.xlsx', '.ppt', '.pptx', '.csv', '.md')

def scan_file(path, source_label):
    """Scan a single file and return its manifest entry."""
    try:
        stat = os.stat(path)
    except (IOError, OSError):
        return None

    # Skip symlinks, sockets, etc.
    if not os.path.isfile(path):
        return None

    entry = {
        'path': path,
        'source': source_label,
        'size': stat.st_size,
        'mtime': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        'type': 'other',
    }

    # Classify
    if is_image(path):
        entry['type'] = 'image'
        entry['exif_date'] = get_exif_date(path)
    elif is_video(path):
        entry['type'] = 'video'
    elif is_document(path):
        entry['type'] = 'document'

    # Hash (the expensive part)
    entry['sha256'] = sha256_file(path)

    return entry

def load_existing(output_path):
    """Load already-scanned paths from an existing manifest (for --resume)."""
    seen = set()
    if os.path.exists(output_path):
        with open(output_path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    seen.add(entry['path'])
                except (json.JSONDecodeError, KeyError):
                    continue
    return seen

def main():
    parser = argparse.ArgumentParser(description='Manifest Agent — file profiler')
    parser.add_argument('scan_path', help='Directory or drive to scan')
    parser.add_argument('--output', '-o', required=True, help='Output manifest file (.jsonl)')
    parser.add_argument('--source', '-s', default=None, help='Source label (default: scan path)')
    parser.add_argument('--resume', action='store_true', help='Resume from existing manifest')
    parser.add_argument('--no-hash', action='store_true', help='Skip hashing (fast metadata-only scan)')
    args = parser.parse_args()

    scan_path = os.path.abspath(args.scan_path)
    source_label = args.source or scan_path

    if not os.path.isdir(scan_path):
        print(f"Error: {scan_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Load existing for resume
    seen = load_existing(args.output) if args.resume else set()
    mode = 'a' if args.resume else 'w'

    print(f"Scanning: {scan_path}", file=sys.stderr)
    print(f"Output: {args.output}", file=sys.stderr)
    if seen:
        print(f"Resuming, {len(seen)} files already scanned", file=sys.stderr)

    count = 0
    skipped = 0
    errors = 0

    with open(args.output, mode) as out:
        for root, dirs, files in os.walk(scan_path):
            # Skip hidden system dirs that cause noise
            dirs[:] = [d for d in dirs if not d.startswith('.Spotlight-V100')
                       and not d.startswith('.fseventsd')
                       and not d.startswith('.Trashes')]
            for fname in files:
                fpath = os.path.join(root, fname)
                if fpath in seen:
                    skipped += 1
                    continue

                if args.no_hash:
                    # Fast path: metadata only
                    try:
                        stat = os.stat(fpath)
                        entry = {
                            'path': fpath,
                            'source': source_label,
                            'size': stat.st_size,
                            'mtime': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                            'type': 'image' if is_image(fpath) else
                                    'video' if is_video(fpath) else
                                    'document' if is_document(fpath) else 'other',
                            'sha256': None,
                        }
                    except (IOError, OSError):
                        errors += 1
                        continue
                else:
                    entry = scan_file(fpath, source_label)
                    if entry is None:
                        errors += 1
                        continue

                out.write(json.dumps(entry) + '\n')
                count += 1

                if count % 100 == 0:
                    print(f"  ... {count} files scanned", file=sys.stderr)

    print(f"Done: {count} new, {skipped} skipped, {errors} errors", file=sys.stderr)

if __name__ == '__main__':
    main()

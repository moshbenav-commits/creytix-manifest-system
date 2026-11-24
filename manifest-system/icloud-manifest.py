#!/usr/bin/env python3
"""
iCloud Manifest Agent — scans local iCloud Drive folder.

On macOS, iCloud Drive syncs to ~/Library/Mobile Documents/com~apple~CloudDocs/.
This script is a thin wrapper around manifest-agent.py targeting that folder.

If iCloud Drive is not synced locally, files will show as placeholders.
Run on the Mac where iCloud Drive is enabled.

Usage:
  python3 icloud-manifest.py --output /tmp/icloud.manifest.jsonl
"""

import os
import sys
import argparse
import subprocess

# iCloud Drive local path on macOS
ICLOUD_PATH = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs")

def main():
    parser = argparse.ArgumentParser(description='iCloud Manifest Agent')
    parser.add_argument('--output', '-o', required=True, help='Output manifest JSONL')
    parser.add_argument('--source', '-s', default='icloud', help='Source label')
    parser.add_argument('--no-hash', action='store_true', help='Skip hashing (fast)')
    args = parser.parse_args()

    if not os.path.isdir(ICLOUD_PATH):
        print(f"Error: iCloud Drive not found at {ICLOUD_PATH}", file=sys.stderr)
        print("Make sure iCloud Drive is enabled and synced on this Mac.", file=sys.stderr)
        sys.exit(1)

    # Delegate to manifest-agent.py
    agent = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manifest-agent.py')
    cmd = [
        sys.executable, agent, ICLOUD_PATH,
        '--output', args.output,
        '--source', args.source,
    ]
    if args.no_hash:
        cmd.append('--no-hash')

    print(f"Scanning iCloud Drive: {ICLOUD_PATH}", file=sys.stderr)
    result = subprocess.run(cmd)
    sys.exit(result.returncode)

if __name__ == '__main__':
    main()

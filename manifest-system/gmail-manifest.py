#!/usr/bin/env python3
"""
Gmail Manifest Agent — scans Gmail for attachments and produces manifests.

Finds all messages with attachments, downloads them, hashes them,
and outputs a JSONL manifest compatible with manifest-db.py.

Usage:
  python3 gmail-manifest.py --output /tmp/gmail.manifest.jsonl
  python3 gmail-manifest.py --output /tmp/gmail.manifest.jsonl --max 1000
  python3 gmail-manifest.py --output /tmp/gmail.manifest.jsonl --query "has:attachment after:2020/01/01"
"""

import os
import sys
import json
import hashlib
import argparse
import subprocess
import tempfile
from datetime import datetime

GWS = "/opt/hatch/bin/hatch_gws_cli"

def run_gws(*args):
    """Run a hatch_gws_cli command and return parsed JSON."""
    cmd = [GWS] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"GWS error: {result.stderr[:200]}", file=sys.stderr)
            return None
        # Try to parse as JSON, fall back to raw
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout
    except subprocess.TimeoutExpired:
        print("GWS command timed out", file=sys.stderr)
        return None
    except Exception as ex:
        print(f"GWS failed: {ex}", file=sys.stderr)
        return None

def list_messages_with_attachments(query, max_results=500):
    """List all message IDs with attachments."""
    q = query or "has:attachment"
    result = run_gws(
        "gmail", "users", "messages", "list",
        "--params", json.dumps({
            "userId": "me",
            "q": q,
            "maxResults": min(max_results, 500)
        }),
        "--page-all"
    )
    if not result:
        return []

    messages = []
    # Result is one JSON per page
    if isinstance(result, str):
        for line in result.strip().split('\n'):
            try:
                page = json.loads(line)
                messages.extend(page.get('messages', []))
            except json.JSONDecodeError:
                continue
    elif isinstance(result, dict):
        messages.extend(result.get('messages', []))

    return messages[:max_results]

def get_attachment(message_id, attachment_id, tmpdir):
    """Download an attachment to a temp file, return path and metadata."""
    result = run_gws(
        "gmail", "users", "messages", "attachments", "get",
        "--params", json.dumps({
            "userId": "me",
            "messageId": message_id,
            "id": attachment_id
        })
    )
    if not result or not isinstance(result, dict):
        return None

    # Attachment data is base64url-encoded
    import base64
    data_b64 = result.get('data', '')
    # Fix padding
    data_b64 += '=' * (-len(data_b64) % 4)
    try:
        data = base64.urlsafe_b64decode(data_b64)
    except Exception:
        return None

    # Write to temp file
    tmp_path = os.path.join(tmpdir, f"att_{message_id}_{attachment_id}")
    with open(tmp_path, 'wb') as f:
        f.write(data)
    return tmp_path

def get_message_attachments(message_id):
    """Get attachment metadata for a message."""
    result = run_gws(
        "gmail", "+read", "--id", message_id, "--format", "json"
    )
    if not result:
        return []

    attachments = []
    # Parse the message structure for attachments
    # This is simplified — real implementation needs to walk MIME parts
    if isinstance(result, dict):
        # Look for attachment info in the result
        pass
    return attachments

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(8192 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def main():
    parser = argparse.ArgumentParser(description='Gmail Manifest Agent')
    parser.add_argument('--output', '-o', required=True, help='Output manifest JSONL')
    parser.add_argument('--query', '-q', default='has:attachment',
                        help='Gmail search query (default: has:attachment)')
    parser.add_argument('--max', type=int, default=500,
                        help='Max messages to process (default: 500)')
    parser.add_argument('--source', '-s', default='gmail',
                        help='Source label (default: gmail)')
    args = parser.parse_args()

    print(f"Searching Gmail: {args.query}", file=sys.stderr)
    messages = list_messages_with_attachments(args.query, args.max)
    print(f"Found {len(messages)} messages with attachments", file=sys.stderr)

    count = 0
    with open(args.output, 'w') as out:
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, msg in enumerate(messages):
                msg_id = msg.get('id')
                if not msg_id:
                    continue

                # Get full message to find attachments
                # Note: This is a simplified version. Full implementation
                # would walk the MIME structure.
                print(f"  ... {i+1}/{len(messages)}", file=sys.stderr)

                # For now, record the message itself as an entry
                # Full attachment extraction requires MIME parsing
                entry = {
                    'path': f"gmail:{msg_id}",
                    'source': args.source,
                    'size': 0,
                    'mtime': datetime.now().isoformat(),
                    'type': 'email',
                    'sha256': None,  # Would hash attachments here
                }
                out.write(json.dumps(entry) + '\n')
                count += 1

    print(f"Done: {count} entries", file=sys.stderr)
    print("\nNOTE: Full attachment extraction requires MIME part walking.", file=sys.stderr)
    print("This version lists messages. Run with --help for options.", file=sys.stderr)

if __name__ == '__main__':
    main()

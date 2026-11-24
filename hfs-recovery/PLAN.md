# HFS+ Recovery — Plan

## What it is

Research and prototyping toward a DiskWarrior-style HFS+ catalog rebuilder: read-only Python tooling that parses damaged HFS+ volumes from raw disk bytes and reconstructs the catalog. All work is our own synthesis of Apple's TN1150 "HFS Plus Volume Format" spec plus notes on open-source HFS+ readers. This project never writes to a disk device.

## Status

- README.md with full volume-layout documentation (volume header, allocation, extents, B-trees) done.
- `hfs_parse.py`, `scan_nodes.py` parsers built; `build_test_image.py`, `test_damage.py`, `test_hfs.py` test suite; GAPS.md tracking open items.
- TBD: how far parsing/reconstruction currently gets on real damage scenarios; which gaps in GAPS.md remain.

## Next

1. Re-run the test suite, close out what GAPS.md lists as open.
2. Test against a real (image-level) damaged volume when one is available.
3. Decide whether this becomes a MacCare component or stays R&D.

## Commit schedule

Commit after every work session. Push before ending the day. Tag milestones (v0.1…). Keep this PLAN.md current.

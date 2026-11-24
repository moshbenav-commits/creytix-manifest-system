#!/usr/bin/env python3
"""
Manifest Pipeline Orchestrator.

Keeps the manifest ledger (manifests.db) updated and clean across
the MacBook Pro (local) and the fleet (remote over SSH).

Runs ON the Pro — that's where manifests.db lives.

Usage:
  python3 pipeline.py scan [target]     # run manifest-agent (with --resume) on target(s)
  python3 pipeline.py scan --all        # scan every enabled target
  python3 pipeline.py collect           # scp fleet JSONLs -> incoming/
  python3 pipeline.py ingest [target]   # ingest collected manifests into DB
  python3 pipeline.py ingest --all      # ingest all known manifests
  python3 pipeline.py report            # ledger summary (read-only)
  python3 pipeline.py clean             # integrity check + vacuum (read-only-ish)
  python3 pipeline.py clean --prune <source> --confirm   # drop a stale source
  python3 pipeline.py full              # scan -> collect -> ingest -> report

Idempotent: scan always uses --resume; ingest uses INSERT OR REPLACE;
collect overwrites; all steps are safe to re-run.
"""

import os
import sys
import shlex
import sqlite3
import subprocess
import argparse
import logging
from datetime import datetime

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required (pip install pyyaml)", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "pipeline-config.yaml")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def setup_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def all_targets(cfg):
    """Yield (kind, target_dict) for every enabled target."""
    for t in cfg.get("local", []) or []:
        if t.get("enabled", True):
            yield ("local", t)
    for t in cfg.get("fleet", []) or []:
        if t.get("enabled", True):
            yield ("fleet", t)


def find_target(cfg, name):
    for kind, t in all_targets(cfg):
        if t["name"] == name:
            return kind, t
    return None, None


# --------------------------------------------------------------------------
# Shell helpers
# --------------------------------------------------------------------------
def run(cmd, **kw):
    """Run a local command, log it, return CompletedProcess."""
    logging.info("+ %s", cmd if isinstance(cmd, str) else shlex.join(cmd))
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                          text=True, **kw)


def ssh_run(cfg, machine, remote_cmd, timeout=60):
    """Run a command on a fleet machine via SSH."""
    opts = cfg["settings"].get("ssh_opts", "")
    cmd = f"ssh {opts} {shlex.quote(machine)} {shlex.quote(remote_cmd)}"
    return run(cmd, timeout=timeout)


def manifest_line_count(path):
    """Count lines in a local manifest file (0 if missing)."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except (IOError, OSError):
        return 0


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def cmd_scan(cfg, args):
    targets = []
    if args.all:
        targets = list(all_targets(cfg))
    elif args.target:
        kind, t = find_target(cfg, args.target)
        if not t:
            print(f"Unknown target: {args.target}", file=sys.stderr)
            print("Known targets:", ", ".join(t["name"] for _, t in all_targets(cfg)))
            sys.exit(1)
        targets = [(kind, t)]
    else:
        print("Specify a target or --all", file=sys.stderr)
        sys.exit(1)

    settings = cfg["settings"]
    for kind, t in targets:
        name = t["name"]
        if kind == "local":
            path = t.get("path")
            if not path:
                logging.warning("[%s] skipped: no path configured (pending drive?)", name)
                continue
            out = t["output"]
            agent = settings["agent_path"]
            cmd = (
                f"nohup python3 {shlex.quote(agent)} {shlex.quote(path)} "
                f"--output {shlex.quote(out)} "
                f"--source {shlex.quote(t['source_label'])} --resume "
                f"> /tmp/manifest-{name}.log 2>&1 & echo $!"
            )
            r = run(cmd)
            if r.returncode == 0:
                logging.info("[%s] scan started (PID %s), log /tmp/manifest-%s.log",
                             name, r.stdout.strip(), name)
            else:
                logging.error("[%s] failed to start: %s", name, r.stderr.strip())
        else:
            machine, user = t["machine"], t["user"]
            agent = t.get("agent_path") or f"/Users/{user}/manifest-agent.py"
            remote_cmd = (
                f"test -f {shlex.quote(agent)} && "
                f"nohup python3 {shlex.quote(agent)} {shlex.quote(t['path'])} "
                f"--output {shlex.quote(t['remote_output'])} "
                f"--source {shlex.quote(t['source_label'])} --resume "
                f"> /tmp/manifest-{name}.log 2>&1 & echo $! "
                f"|| echo AGENT_MISSING"
            )
            r = ssh_run(cfg, machine, remote_cmd)
            out = r.stdout.strip()
            if "AGENT_MISSING" in out:
                logging.error("[%s] manifest-agent.py not found at %s on %s",
                              name, agent, machine)
            elif r.returncode == 0:
                logging.info("[%s] scan started on %s (PID %s)", name, machine, out)
            else:
                logging.error("[%s] ssh failed: %s", name, r.stderr.strip() or out)


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------
def cmd_collect(cfg, args):
    settings = cfg["settings"]
    incoming = settings["incoming_dir"]
    os.makedirs(incoming, exist_ok=True)
    opts = settings.get("ssh_opts", "")

    collected = []
    for kind, t in all_targets(cfg):
        if kind != "fleet":
            continue
        name = t["name"]
        machine, user = t["machine"], t["user"]
        src = f"{user}@{machine}:{t['remote_output']}"
        dst = os.path.join(incoming, f"{name}.manifest.jsonl")
        cmd = f"scp {opts} {shlex.quote(src)} {shlex.quote(dst)}"
        r = run(cmd, timeout=600)
        if r.returncode == 0 and os.path.exists(dst):
            n = manifest_line_count(dst)
            logging.info("[%s] collected %s (%d lines)", name, dst, n)
            collected.append((name, dst, n))
        else:
            logging.error("[%s] collect failed: %s", name, r.stderr.strip()[:200])
    logging.info("Collected %d/%d fleet manifests", len(collected),
                 sum(1 for k, _ in all_targets(cfg) if k == "fleet"))
    return collected


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------
def known_manifest_files(cfg):
    """Map target name -> local manifest path (local outputs + incoming/)."""
    settings = cfg["settings"]
    incoming = settings["incoming_dir"]
    files = {}
    for kind, t in all_targets(cfg):
        if kind == "local":
            files[t["name"]] = t["output"]
        else:
            files[t["name"]] = os.path.join(incoming, f"{t['name']}.manifest.jsonl")
    return files


def cmd_ingest(cfg, args):
    settings = cfg["settings"]
    db_tool = settings["db_path"]
    files = known_manifest_files(cfg)

    if args.all:
        names = list(files.keys())
    elif args.target:
        if args.target not in files:
            print(f"Unknown target: {args.target}", file=sys.stderr)
            sys.exit(1)
        names = [args.target]
    else:
        print("Specify a target or --all", file=sys.stderr)
        sys.exit(1)

    for name in names:
        path = files[name]
        if not os.path.exists(path):
            logging.warning("[%s] no manifest at %s, skipping", name, path)
            continue
        n = manifest_line_count(path)
        logging.info("[%s] ingesting %s (%d lines)...", name, path, n)
        r = run([sys.executable, db_tool, "ingest", path], timeout=7200)
        if r.returncode == 0:
            logging.info("[%s] %s", name, r.stdout.strip() or r.stderr.strip())
        else:
            logging.error("[%s] ingest failed: %s", name, r.stderr.strip()[:500])


# --------------------------------------------------------------------------
# report (read-only)
# --------------------------------------------------------------------------
def db_path_for(cfg):
    return os.path.join(os.path.dirname(os.path.abspath(cfg["settings"]["db_path"])),
                        "manifests.db")


def cmd_report(cfg, args):
    dbp = db_path_for(cfg)
    if not os.path.exists(dbp):
        print(f"Database not found: {dbp}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
    no_hash = conn.execute("SELECT COUNT(*) FROM files WHERE sha256 IS NULL").fetchone()[0]

    print("=" * 60)
    print("MANIFEST LEDGER SUMMARY")
    print("=" * 60)
    print(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    print(f"Database:  {dbp}")
    print()
    print(f"Total files: {total[0]:,}")
    print(f"Total size:  {total[1]:,} bytes ({total[1]/1e12:.2f} TB)")
    print(f"Files without hash: {no_hash:,}")

    print("\nBy source:")
    for s in conn.execute(
            "SELECT source, COUNT(*) c, COALESCE(SUM(size),0) sz "
            "FROM files GROUP BY source ORDER BY sz DESC"):
        print(f"  {s['source']}: {s['c']:,} files, {s['sz']/1e9:.1f} GB")

    print("\nBy type:")
    for t in conn.execute(
            "SELECT type, COUNT(*) c, COALESCE(SUM(size),0) sz "
            "FROM files GROUP BY type ORDER BY sz DESC"):
        print(f"  {t['type'] or 'unknown'}: {t['c']:,} files, {t['sz']/1e9:.1f} GB")

    # Duplicate aggregates (no per-file listing — fast on 9M+ rows)
    dup = conn.execute("""
        SELECT COUNT(*) groups, COALESCE(SUM((n-1)*sz),0) reclaim
        FROM (SELECT sha256, COUNT(*) n, MAX(size) sz
              FROM files WHERE sha256 IS NOT NULL
              GROUP BY sha256 HAVING n > 1)
    """).fetchone()
    print(f"\nDuplicate groups: {dup['groups']:,}")
    print(f"Reclaimable: {dup['reclaim']:,} bytes ({dup['reclaim']/1e9:.1f} GB)")

    print("\nTop 10 duplicate groups by wasted space:")
    for d in conn.execute("""
        SELECT n, sz, (n-1)*sz wasted
        FROM (SELECT sha256, COUNT(*) n, MAX(size) sz
              FROM files WHERE sha256 IS NOT NULL
              GROUP BY sha256 HAVING n > 1)
        ORDER BY wasted DESC LIMIT 10
    """):
        print(f"  {d['n']}x copies of {d['sz']:,} bytes -> wastes {d['wasted']:,} bytes")

    # Configured vs. present sources
    configured = {t["source_label"] for _, t in all_targets(cfg)}
    present = {r[0] for r in conn.execute("SELECT DISTINCT source FROM files")}
    stale = sorted(present - configured)
    missing = sorted(configured - present)
    if stale:
        print(f"\nStale sources in DB (not in config): {', '.join(stale)}")
    if missing:
        print(f"Configured sources with no rows yet: {', '.join(missing)}")

    conn.close()
    logging.info("report complete")


# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------
def cmd_clean(cfg, args):
    dbp = db_path_for(cfg)
    if not os.path.exists(dbp):
        print(f"Database not found: {dbp}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(dbp)
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"integrity_check: {integ}")
    logging.info("integrity_check: %s", integ)
    if integ != "ok":
        print("Database integrity FAILED — not proceeding.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    configured = {t["source_label"] for _, t in all_targets(cfg)}
    present = [r[0] for r in conn.execute("SELECT DISTINCT source FROM files")]
    stale = sorted(set(present) - configured)
    if stale:
        print(f"Stale sources (in DB, not in config): {', '.join(stale)}")
    else:
        print("No stale sources.")

    if args.prune:
        if args.prune not in present:
            print(f"Source '{args.prune}' not in DB.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        if not args.confirm:
            print(f"Refusing to prune '{args.prune}' without --confirm.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        n = conn.execute("SELECT COUNT(*) FROM files WHERE source=?", (args.prune,)).fetchone()[0]
        conn.execute("DELETE FROM files WHERE source=?", (args.prune,))
        conn.commit()
        logging.warning("pruned source '%s' (%d rows)", args.prune, n)
        print(f"Pruned '{args.prune}': {n:,} rows deleted.")

    conn.execute("VACUUM")
    conn.close()
    print("VACUUM complete.")
    logging.info("clean complete")


# --------------------------------------------------------------------------
# full
# --------------------------------------------------------------------------
def cmd_full(cfg, args):
    logging.info("=== PIPELINE FULL RUN START ===")
    args_all = argparse.Namespace(all=True, target=None)
    cmd_scan(cfg, args_all)
    logging.info("--- scan launched (background); collecting current manifests ---")
    cmd_collect(cfg, args)
    cmd_ingest(cfg, args_all)
    cmd_report(cfg, args)
    logging.info("=== PIPELINE FULL RUN COMPLETE ===")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Manifest pipeline orchestrator")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Run manifest-agent on target(s) with --resume")
    p.add_argument("target", nargs="?", help="Target name from config")
    p.add_argument("--all", action="store_true", help="Scan all enabled targets")

    sub.add_parser("collect", help="SCP fleet JSONLs to Pro incoming/")

    p = sub.add_parser("ingest", help="Ingest manifests into manifests.db")
    p.add_argument("target", nargs="?", help="Target name from config")
    p.add_argument("--all", action="store_true", help="Ingest all known manifests")

    sub.add_parser("report", help="Ledger summary (read-only)")

    p = sub.add_parser("clean", help="Integrity check, stale-source report, vacuum")
    p.add_argument("--prune", metavar="SOURCE", help="Delete all rows for a source label")
    p.add_argument("--confirm", action="store_true", help="Required to actually prune")

    sub.add_parser("full", help="scan -> collect -> ingest -> report")

    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg["settings"]["log_file"])

    logging.info("pipeline %s invoked", args.command)
    if args.command == "scan":
        cmd_scan(cfg, args)
    elif args.command == "collect":
        cmd_collect(cfg, args)
    elif args.command == "ingest":
        cmd_ingest(cfg, args)
    elif args.command == "report":
        cmd_report(cfg, args)
    elif args.command == "clean":
        cmd_clean(cfg, args)
    elif args.command == "full":
        cmd_full(cfg, args)


if __name__ == "__main__":
    main()

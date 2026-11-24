# manifest-pipeline (MacCare)

Scan drives and machines, build one unified manifest ledger, find
duplicates, and keep the ledger clean. The pipeline is the "brain" for
organizing files across many drives: it records *what is where* (path,
size, SHA256, type, EXIF date) and answers *what is duplicated*.

Commercial-clean: **Python standard library only.** No PyYAML, no
third-party packages. Pillow is an *optional, unbundled* dependency of
`manifest-agent.py` — if it is installed, images also get an EXIF date;
if it is not, the agent works fine without it.

## Components

| File | What it does |
|---|---|
| `manifest-pipeline` | Orchestrator: scan → collect → ingest → report → clean. Run this. |
| `manifest-agent.py` | Scans one directory/drive, writes a JSONL manifest (one object per file: path, size, mtime, SHA256, type, EXIF date). Resumable with `--resume`. Read-only: never modifies source files. |
| `manifest-db.py` | SQLite ledger (`manifests.db`): ingest JSONL manifests, find duplicates by hash, stats, find-by-hash. |
| `CONFIG_EXAMPLE.yaml` | Example config. Copy to `pipeline-config.yaml` and edit. |
| `LICENSE` | MIT — original work. |

## Quick start (standalone, local only)

```bash
cd src/manifest-pipeline
chmod +x manifest-pipeline

# 1. Copy the example config and point it at a directory to scan
cp CONFIG_EXAMPLE.yaml pipeline-config.yaml
# ... edit pipeline-config.yaml: set a local target's path + enabled: true ...

# 2. Scan (foreground, so the command waits for the scan to finish)
./manifest-pipeline --local-only scan My-Target --foreground

# 3. Ingest the manifest into the ledger
./manifest-pipeline --local-only ingest My-Target

# 4. Report (read-only): totals, by source/type, duplicate groups
./manifest-pipeline --local-only report

# 5. Clean: integrity check + vacuum (read-only unless --prune --confirm)
./manifest-pipeline --local-only clean
```

One command for the whole cycle (background scans, then collect/ingest/report):

```bash
./manifest-pipeline --local-only full
```

## Commands

| Command | Effect |
|---|---|
| `scan [target]` / `scan --all` | Run `manifest-agent.py --resume` on target(s). Local scans launch in the background (log: `/tmp/manifest-<name>.log`); add `--foreground` to wait. Fleet scans run over SSH. |
| `collect` | SCP fleet manifests into `incoming/`. Skipped with `--local-only` or when `scp` is absent. |
| `ingest [target]` / `ingest --all` | Load manifest(s) into `manifests.db` (`INSERT OR REPLACE`, safe to re-run). |
| `report` | Ledger summary, read-only. Totals, per-source/per-type, duplicate groups + reclaimable bytes. |
| `clean` | `PRAGMA integrity_check`, stale-source report, `VACUUM`. `--prune <source> --confirm` deletes one source label's rows (refuses without `--confirm`). |
| `full` | `scan` → `collect` → `ingest` → `report`. |

Global flags (before the command): `--config PATH`, `--local-only`,
`--db PATH`.

## Config

`pipeline-config.yaml` next to the script, or any file passed with
`--config`. YAML subset or JSON. Format:

```yaml
settings:
  incoming_dir: incoming        # fleet manifests land here
  agent_path: manifest-agent.py # relative = next to the config file
  db_path: manifest-db.py       # ledger lives next to this file
  log_file: pipeline.log
  ssh_opts: "-o BatchMode=yes -o ConnectTimeout=15"

local:
  - name: My-Drive
    path: "/Volumes/My Drive"
    source_label: My-Drive      # stable identity — never rename lightly
    output: runs/my-drive.manifest.jsonl
    enabled: true

fleet:
  - name: other-mac-users
    machine: fleet-other
    user: macadmin
    path: /Users/macadmin
    source_label: other-mac-users
    remote_output: /Users/macadmin/users.manifest.jsonl
    agent_path: /Users/macadmin/manifest-agent.py
    enabled: true
```

The built-in YAML parser supports comments, nested mappings, lists of
mappings, quoted/plain scalars, booleans, numbers. It does **not**
support anchors, aliases, multi-line scalars, flow collections, or tabs.
Use JSON config if you need those.

## Local-only mode

`--local-only` skips every fleet target: no SSH, no SCP, no fleet rows.
Fleet steps are also skipped gracefully (with a warning, not an error)
when `ssh`/`scp` is not installed. A machine with no fleet access can
still run the full local pipeline.

## Testing without touching a real ledger

```bash
./manifest-pipeline --config test-config.yaml --db /tmp/test/manifests.db \
  --local-only scan Test-Target --foreground
./manifest-pipeline --config test-config.yaml --db /tmp/test/manifests.db \
  --local-only ingest Test-Target
./manifest-pipeline --config test-config.yaml --db /tmp/test/manifests.db \
  --local-only report
./manifest-pipeline --config test-config.yaml --db /tmp/test/manifests.db \
  --local-only clean
```

`--db` redirects the ledger for the orchestrator's own `report`/`clean`
and for the `manifest-db.py` subprocesses it spawns (via
`MANIFEST_DB_PATH`). `manifest-db.py` also honours `MANIFEST_DB_PATH`
directly when run on its own.

## The tools on their own

```bash
# Scan a drive straight to a manifest (no orchestrator, no config)
python3 manifest-agent.py /Volumes/MyDrive --output /tmp/mydrive.manifest.jsonl
python3 manifest-agent.py /Volumes/MyDrive --output /tmp/mydrive.manifest.jsonl --resume
python3 manifest-agent.py /Volumes/MyDrive --output /tmp/mydrive.manifest.jsonl --no-hash

# Ledger directly
python3 manifest-db.py init
python3 manifest-db.py ingest /tmp/mydrive.manifest.jsonl
python3 manifest-db.py duplicates
python3 manifest-db.py stats
python3 manifest-db.py find <sha256>
```

## Design principles

- **Manifests are portable**: JSONL files can be generated on any machine and shipped back.
- **Resumable**: interrupted scans pick up where they left off (`--resume`).
- **Hash-verified**: every dedupe decision is backed by SHA256.
- **Read-only scanning**: the agent never modifies source files.
- **Idempotent**: every pipeline step is safe to re-run.
- **Local-first**: works fully with no network and no fleet; remote features degrade gracefully.

## License

MIT — original work. See `LICENSE`. Third-party notices: see the kit's
`THIRD-PARTY-NOTICES.md`. NudeNet is excluded from the MacCare commercial
bundle (AGPL) and is not part of this package.

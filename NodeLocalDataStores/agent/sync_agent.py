#!/usr/bin/env python3
#
# Copyright 2026 Esri
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Node-local dataset sync agent for ArcGIS Enterprise on Kubernetes routing and
geocoding nodes.

Sample code, provided as-is and unsupported.

The agent runs as a DaemonSet on a dedicated node pool. On each pass it copies
any dataset the node does not yet hold from a shared NFS export onto that node's
local storage, and does three things a plain copy loop does not:

  1. Datasets are copied into a staging directory and moved into place with an
     atomic rename, so a partially transferred dataset is never visible under
     the path a service pod reads.
  2. The bootstrap taint is removed from the node after the first successful
     pass, releasing routing and geocoding pods to schedule there.
  3. A generation label is written to the node after every successful pass, so
     publish-time checks and monitoring can see which nodes hold which data.

One dataset is copied with several concurrent transfers rather than one. A
single-threaded copier issues one read at a time and tops out well below what
the export can deliver, no matter how the storage is tuned, so concurrency in
the agent is the only remaining lever. See sync_dataset.

Directory layout under --dest-root:

    staging/<dataset>/    transient, never mounted by service pods
    active/<dataset>/     mounted read-only by routing and geocoding pods

Source model:

    Each immediate subdirectory of --source is one immutable dataset. Datasets
    are added, never modified in place. New data means a new directory and a
    republish of the dependent service. The agent therefore syncs only
    datasets that are absent from active/ and leaves existing ones untouched,
    which is what keeps it from writing into a directory a running pod is
    reading.

    A dataset directory is typically a few hundred gigabytes of extracted data
    beside a split zip archive of that same data. The archive is dropped with
    --exclude; it is a transfer format and no service reads it.

Transfer engine: rclone
-----------------------
The copy is done by `rclone sync`. A whole-file copier such as rsync cannot
reach the same throughput on this shape of data however it is parallelised,
because it moves whole files one thread at a time and the largest member of a
typical dataset is a single file of well over a hundred gigabytes. rclone issues
concurrent ranged reads within a file, which cuts a several-hundred-gigabyte
sync from roughly twenty minutes to roughly five on the test cluster. Every flag
below was chosen from a measurement rather than from taste; see
../tuning/rclone.md.

Hardlinking against a previous generation is deliberately not implemented.
rclone has no equivalent of rsync --link-dest, and it would save nothing here:
the payload is monolithic multi-gigabyte geodatabases that differ every release,
and a re-export with fresh timestamps defeats hardlinking outright. Size volumes
for two full generations.

Usage:
    sync_agent.py --source /mnt/source --dest-root /mnt/data --interval 300

Environment:
    NODE_NAME   name of the node this pod runs on (downward API, required
                unless --no-node-patch is set)

Requires rclone and kubectl on PATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("sync-agent")

_shutdown = threading.Event()

# Label and taint keys are namespaced under a DNS domain you control. This must
# match the domain used in the manifests, which template it as __LABEL_DOMAIN__.
LABEL_DOMAIN = os.environ.get("LABEL_DOMAIN", "geodata.example.com")

BOOTSTRAP_TAINT = f"{LABEL_DOMAIN}/data-not-ready"
GENERATION_LABEL = f"{LABEL_DOMAIN}/dataset-generation"
DATASET_COUNT_LABEL = f"{LABEL_DOMAIN}/dataset-count"

# Measured rather than chosen: 8 x 8 reached roughly 2000 MB/s on a cold read
# and saturated provisioned IOPS. See ../tuning/rclone.md before changing them.
DEFAULT_TRANSFERS = 8
DEFAULT_STREAMS = 8

# rclone --stats-one-line writes "300 KiB / 300 KiB, 100%, 0 B/s, ETA -" to
# stderr. The multi-line form labels this "Transferred:"; the one-line form does
# not, so the "X / Y, N%" shape is what identifies it.
_RCLONE_XFER = re.compile(
    r"(\d[\d.]*)\s*([KMGTP]?i?B)\s*/\s*[\d.]+\s*[KMGTP]?i?B,\s*\d+%"
)
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2,
          "GiB": 1024 ** 3, "TiB": 1024 ** 4, "PiB": 1024 ** 5}


def handle_signal(signum, frame):  # noqa: ARG001
    _shutdown.set()
    log.info("Shutdown signal received, will exit after the current operation.")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


# --------------------------------------------------------------------------
# Dataset discovery and generation identity
# --------------------------------------------------------------------------

def list_datasets(directory: Path) -> list[str]:
    """Immediate subdirectories of `directory`, sorted. One per dataset."""
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))


def generation_value(datasets: list[str]) -> str:
    """
    Stable short identity for the set of datasets a node holds.

    Node label values are limited to 63 characters, so this is a truncated
    digest rather than the dataset names themselves. Operators read the names
    from the pod log or from active/ directly; the label exists so that
    "which nodes are current" is answerable with a single kubectl query.
    """
    if not datasets:
        return "none"
    digest = hashlib.sha256("\n".join(sorted(datasets)).encode("utf-8")).hexdigest()
    return f"g{digest[:12]}"


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def sweep_staging(staging: Path) -> None:
    """
    Clear the whole staging tree before the first pass.

    Anything under staging/ is by definition an interrupted copy -- a completed
    one was renamed away. Sweeping per-dataset at copy time is not enough: an
    orphan left by a killed pod for a dataset that is no longer pending is
    never revisited, and on this data that silently holds ~507 GiB of the
    node's volume until someone notices.
    """
    if not staging.is_dir():
        return
    for leftover in staging.iterdir():
        log.warning("Removing stale staging directory %s", leftover)
        shutil.rmtree(leftover, ignore_errors=True)


def rclone_transferred_bytes(output: str | None) -> int:
    """
    Bytes moved, from the last "Transferred:" line rclone writes to stderr.

    Zero when nothing matched, which is what a fully resumed pass legitimately
    reports -- so it is not treated as an error.
    """
    matches = _RCLONE_XFER.findall(output or "")
    if not matches:
        return 0
    amount, unit = matches[-1]
    return int(float(amount) * _UNITS.get(unit, 1))


def tree_bytes(root: Path) -> int:
    """Total size of every file under `root`. Verification for the per-pass byte counts."""
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def sync_dataset(source_dir: Path, staging: Path, active: Path, dataset: str,
                 excludes: list[str] | None = None,
                 transfers: int = DEFAULT_TRANSFERS,
                 streams: int = DEFAULT_STREAMS) -> tuple[bool, int, float]:
    """
    Copy one dataset into staging with rclone, then rename it into active.

    Returns (ok, bytes_transferred, seconds).
    """
    src = source_dir / dataset
    stage = staging / dataset
    dest = active / dataset

    # Deliberately NOT wiped between attempts. An earlier version removed the
    # staging directory before every attempt, which threw away the whole copy on
    # any failure -- on this data that is up to 505 GiB and ~40 minutes of the
    # shared FSx throughput budget discarded to re-fetch bytes already on
    # disk. rclone skips files already matching on size and modtime, so a retry
    # sends only what is missing.
    #
    # The killed-pod case is handled in two places:
    # sweep_staging() clears the tree once at startup, and `rclone sync` below
    # removes anything in staging that the source does not have.
    resuming = stage.exists() and any(stage.iterdir())
    if resuming:
        log.info("Resuming into existing staging directory %s", stage)
    stage.mkdir(parents=True, exist_ok=True)

    cmd = ["rclone", "sync", str(src), str(stage),
           # sync rather than copy: it deletes files staging holds that the
           # source does not, which is what clears temp files left by a killed
           # pod. Safe because staging is private to the agent and no service
           # pod ever reads it.
           #
           # --inplace is NOT set and must never be. An interrupted --inplace
           # transfer leaves a
           # full-size file containing a hole -- indistinguishable from a
           # complete copy by any size check.
           "--metadata",            # rsync -a preserved ownership and mode
           "--ignore-checksum",     # verifying re-reads both sides, ~2x cost
           f"--transfers={transfers}",
           f"--multi-thread-streams={streams}",
           "--multi-thread-cutoff=256M",
           "--multi-thread-chunk-size=64M",
           # 128 KiB by default, which the rclone docs flag for filesystems that
           # handle scattered small writes badly -- exactly this access pattern.
           "--multi-thread-write-buffer-size=4M",
           "--checkers=16",
           "--stats=60s", "--stats-one-line", "-v"]

    # the split zip archives beside the extracted dataset are its transfer
    # format and no routing service reads them. Excluding them drops ~171 GiB
    # per node per bootstrap.
    for pattern in excludes or []:
        cmd += ["--exclude", pattern]

    log.info("Syncing dataset %s: %d transfers x %d streams", dataset, transfers, streams)
    started = time.monotonic()
    try:
        result = run(cmd)
    except subprocess.CalledProcessError as exc:
        elapsed = time.monotonic() - started
        # Leave staging in place. The next pass resumes into it rather than
        # re-fetching everything already transferred (the pass retries on the next
        # interval, and nothing partial is visible to a service pod because the
        # rename into active/ only happens on success).
        log.error("rclone failed for %s after %.1fs, keeping staging for resume: %s",
                  dataset, elapsed, (exc.stderr or "").strip()[-2000:])
        return False, 0, elapsed

    elapsed = time.monotonic() - started
    transferred = rclone_transferred_bytes(result.stderr)
    staged = tree_bytes(stage)

    # Atomic within the filesystem. A service pod either sees the complete
    # dataset under active/ or does not see the directory at all.
    try:
        os.rename(stage, dest)
    except OSError as exc:
        # Keep staging: at this point the copy is COMPLETE and only the move
        # failed, so discarding it would throw away a finished 505 GiB transfer
        # over something like a transient EBUSY. The next pass re-verifies it
        # cheaply -- every file matches on size and modtime, so rclone transfers
        # nothing -- and retries the rename.
        log.error("Could not move %s into place, keeping staging for retry: %s",
                  dataset, exc)
        return False, transferred, time.monotonic() - started

    log.info("Dataset %s ready at %s (%d bytes transferred, %d bytes on disk, "
             "%.1fs, %.1f MB/s)",
             dataset, dest, transferred, staged, elapsed,
             staged / elapsed / 1_000_000 if elapsed else 0.0)
    return True, transferred, elapsed


def sync_pass(source: Path, dest_root: Path,
              excludes: list[str] | None = None,
              transfers: int = DEFAULT_TRANSFERS,
              streams: int = DEFAULT_STREAMS) -> tuple[bool, int]:
    """
    One pass over the source. Returns (no_failures, datasets_added).

    Datasets already present in active/ are immutable and are skipped.
    """
    staging = dest_root / "staging"
    active = dest_root / "active"
    staging.mkdir(parents=True, exist_ok=True)
    active.mkdir(parents=True, exist_ok=True)

    available = list_datasets(source)
    present = set(list_datasets(active))
    pending = [d for d in available if d not in present]

    if not available:
        log.warning("Source %s contains no dataset directories", source)

    if not pending:
        log.info("No new datasets. %d active: %s", len(present), ", ".join(sorted(present)) or "-")
        return True, 0

    log.info("%d new dataset(s) to sync: %s", len(pending), ", ".join(pending))

    ok_all, added, total_bytes, total_secs = True, 0, 0, 0.0
    for dataset in pending:
        if _shutdown.is_set():
            log.info("Shutdown requested, stopping before dataset %s", dataset)
            break
        ok, transferred, elapsed = sync_dataset(source, staging, active, dataset,
                                                excludes, transfers, streams)
        total_bytes += transferred
        total_secs += elapsed
        if ok:
            added += 1
        else:
            ok_all = False

    log.info("Pass complete: %d dataset(s) added, %d bytes, %.1fs total",
             added, total_bytes, total_secs)
    return ok_all, added


# --------------------------------------------------------------------------
# Node object
# --------------------------------------------------------------------------

def remove_bootstrap_taint(node: str) -> bool:
    """
    Remove the bootstrap taint. Idempotent: kubectl reports 'not found' when
    the taint is already gone, which is a normal outcome after a pod restart.
    """
    result = run(["kubectl", "taint", "node", node, f"{BOOTSTRAP_TAINT}-"], check=False)
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        log.info("Removed taint %s from node %s", BOOTSTRAP_TAINT, node)
        return True
    if "not found" in stderr.lower():
        log.info("Taint %s already absent from node %s", BOOTSTRAP_TAINT, node)
        return True
    log.error("Could not remove taint from %s: %s", node, stderr)
    return False


def apply_bootstrap_taint(node: str) -> bool:
    """
    Re-apply the bootstrap taint once the node's data has gone.
    --overwrite makes it idempotent.

    NoSchedule matches the startupTaints declaration in the NodePool, and gates new pods
    only: routing pods already running on the node keep running against data
    that is no longer there. Clearing those is a manual step.
    """
    result = run(
        ["kubectl", "taint", "node", node,
         f"{BOOTSTRAP_TAINT}=true:NoSchedule", "--overwrite"],
        check=False,
    )
    if result.returncode != 0:
        log.error("Could not re-apply taint to %s: %s", node, (result.stderr or "").strip())
        return False
    log.warning("Re-applied taint %s to node %s: active/ is empty", BOOTSTRAP_TAINT, node)
    return True


def read_node_labels(node: str) -> dict | None:
    """
    Current labels on the node, or None if they cannot be read.

    None means "unknown", not "empty" -- the caller writes rather than assuming
    the labels are already correct.
    """
    result = run(
        ["kubectl", "get", "node", node, "-o", "jsonpath={.metadata.labels}"],
        check=False,
    )
    if result.returncode != 0:
        log.warning("Could not read labels from node %s: %s", node,
                    (result.stderr or "").strip())
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        log.warning("Could not parse labels from node %s", node)
        return None


def write_generation_label(node: str, datasets: list[str]) -> bool:
    """
    Ensure the node carries the current generation and dataset-count labels
   .

    Read first, write only on a difference. The agent runs a pass every
    interval for the life of the node, and in steady state nothing changes, so
    an unconditional write produced one API write, one audit event and one
    watch notification per node per interval -- forever, and scaling with the
    node count. Observed on this MVP: ~110 identical writes per node in the
    first nine hours.

    Reading first also makes the labels self-healing. If something outside the
    agent removes or edits them, the next pass restores them, which matters
    because this label is what publish-time checks read.
    """
    value = generation_value(datasets)
    count = str(len(datasets))

    current = read_node_labels(node)
    if (current is not None
            and current.get(GENERATION_LABEL) == value
            and current.get(DATASET_COUNT_LABEL) == count):
        log.debug("Node %s already labelled %s=%s (%s datasets), not rewriting",
                  node, GENERATION_LABEL, value, count)
        return True

    result = run(
        ["kubectl", "label", "node", node, "--overwrite",
         f"{GENERATION_LABEL}={value}",
         f"{DATASET_COUNT_LABEL}={count}"],
        check=False,
    )
    if result.returncode != 0:
        log.error("Could not label node %s: %s", node, (result.stderr or "").strip())
        return False
    log.info("Node %s labelled %s=%s (%s datasets)", node, GENERATION_LABEL, value, count)
    return True


def reconcile_node_state(node: str, ok: bool, active_datasets: list[str],
                         taint_removed: bool) -> bool:
    """
    Bring the node's taint and labels into line with what active/ actually
    holds. Returns the new value of `taint_removed`.

    The taint is a bootstrap gate: released once after the first pass that
    leaves the node with data, and re-asserted only when active/ is empty.

    Re-assertion exists for instance-store nodes. Local NVMe is lost on a
    stop or a hardware replacement, not only on termination, and Karpenter's
    startupTaints apply at registration only -- so a node whose data vanished
    under it would otherwise stay schedulable and accept routing pods that have
    nothing to read. An empty active/ after a clean pass means the data is gone,
    not that a copy is still in flight; a pass that failed leaves everything
    untouched, because that is the case where a copy may simply be incomplete.
    """
    if not ok:
        log.warning("Pass had failures; leaving node state unchanged.")
        return taint_removed

    if active_datasets:
        if not taint_removed:
            taint_removed = remove_bootstrap_taint(node)
        write_generation_label(node, active_datasets)
        return taint_removed

    if apply_bootstrap_taint(node):
        taint_removed = False
    # Labels follow the taint down, or the node keeps advertising a generation
    # it no longer has -- and that label is what publish-time checks read.
    write_generation_label(node, active_datasets)
    return taint_removed


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Periodic node-local dataset sync with Kubernetes taint and label management.")
    parser.add_argument("--source", required=True, help="Source directory (ONTAP mount)")
    parser.add_argument("--dest-root", required=True,
                        help="Node-local root containing staging/ and active/")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between passes")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="rsync exclude pattern; repeatable. Used to drop the split "
                             "zip archives that sit beside the extracted dataset.")
    parser.add_argument("--transfers", type=int, default=DEFAULT_TRANSFERS, metavar="N",
                        help=f"rclone concurrent file transfers (default: {DEFAULT_TRANSFERS})")
    parser.add_argument("--multi-thread-streams", type=int, default=DEFAULT_STREAMS,
                        metavar="N", dest="streams",
                        help="rclone concurrent streams within one large file "
                             f"(default: {DEFAULT_STREAMS}). This is what rsync could "
                             "not do and why the target is reachable.")
    parser.add_argument("--no-node-patch", action="store_true",
                        help="Skip taint removal and labelling (local testing)")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.transfers < 1 or args.streams < 1:
        sys.exit("Error: --transfers and --multi-thread-streams must be at least 1")

    source = Path(args.source).resolve()
    dest_root = Path(args.dest_root).resolve()

    if not source.is_dir():
        sys.exit(f"Error: source '{source}' is not a directory")
    dest_root.mkdir(parents=True, exist_ok=True)

    node = os.environ.get("NODE_NAME", "")
    if not args.no_node_patch and not node:
        sys.exit("Error: NODE_NAME is not set. Provide it via the downward API "
                 "or pass --no-node-patch.")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("Source: %s", source)
    log.info("Destination root: %s", dest_root)
    log.info("Node: %s", node or "(node patching disabled)")
    log.info("Interval: %ds", args.interval)
    log.info("Transfers: %d x %d streams", args.transfers, args.streams)
    if args.exclude:
        log.info("Excluding: %s", ", ".join(args.exclude))

    # clear interrupted copies once, before the first pass. Doing this at
    # startup rather than per-dataset is what catches orphans for datasets that
    # are no longer pending.
    sweep_staging(dest_root / "staging")

    taint_removed = False

    while True:
        ok, _added = sync_pass(source, dest_root,
                               excludes=args.exclude,
                               transfers=args.transfers,
                               streams=args.streams)
        active_datasets = list_datasets(dest_root / "active")

        if not args.no_node_patch:
            taint_removed = reconcile_node_state(node, ok, active_datasets, taint_removed)

        if args.once or _shutdown.wait(timeout=args.interval):
            break

    log.info("Stopped.")


if __name__ == "__main__":
    main()

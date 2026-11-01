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

The agent runs as a DaemonSet on a dedicated node pool. On every pass it runs
one recursive ``rclone sync`` from the authoritative ONTAP source directly into
``datasetRoot`` (the ``--dest-root`` argument). Source additions and changes are
copied, and destination paths absent from the source are deleted, subject to
configured excludes.

rclone compares files by size and modification time by default. It transfers a
changed file to a temporary path and renames the completed file into place;
``--inplace`` is deliberately not used. This gives per-file replacement rather
than whole-tree atomicity: consumers can observe a mixture of old and new files
while a pass is running, and files completed before an interrupted pass remain
visible. A later successful pass reconciles the direct mirror. The agent does
not maintain a second full staging tree.

After a successful pass, the bootstrap taint is removed and node labels record
a digest and count of the top-level dataset directory names in ``datasetRoot``.
The generation label identifies those names, not their file contents. If the
dataset root is absent or empty, the bootstrap taint is applied again. If it
exists but cannot be read, the inventory is unknown rather than empty, and the
taint and labels are left exactly as they are.

The transfer uses concurrent files and concurrent ranged reads within large
files. A single-threaded copier cannot use the measured ONTAP and local-storage
throughput for this dataset shape; see ../tuning/rclone.md before changing the
concurrency flags.

A dataset directory is typically a few hundred gigabytes of extracted data
beside split zip archives. Configured ``--exclude`` patterns can omit those
transfer artifacts because routing services do not read them.

Usage:
    sync_agent.py --source /mnt/source --dest-root /mnt/geodata --interval 300

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
# match the chart's `labelDomain` value.
LABEL_DOMAIN = os.environ.get("LABEL_DOMAIN", "geodata.example.com")

BOOTSTRAP_TAINT = f"{LABEL_DOMAIN}/data-not-ready"

# Applied when datasetRoot exists but cannot be read, which on Bottlerocket
# means the tree was SELinux-relabeled by a relabeling volume and is permanently
# denied to this agent and to every other consumer on the node. The value names
# the remedy because that is what the operator sees in `kubectl describe node`,
# and node replacement is the only remedy: chcon is denied to a sysadmin debug
# container and restorecon is not present. Distinct from BOOTSTRAP_TAINT so that
# "unreadable" is never confused with "genuinely empty" -- those need opposite
# responses, and only this one means the node has to go.
UNREADABLE_TAINT = f"{LABEL_DOMAIN}/data-unreadable"
UNREADABLE_TAINT_VALUE = "replace-node"

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


def run(cmd: list[str], check: bool = True, *,
        stream_stderr: bool = False) -> subprocess.CompletedProcess:
    """
    Run `cmd` and return a CompletedProcess.

    ``stream_stderr`` is opt-in and defaults to the buffered
    ``capture_output=True`` behavior, so every kubectl call is byte-for-byte
    unchanged. Those callers already log stderr selectively --
    ``remove_bootstrap_taint`` treats "not found" as a normal info-level outcome
    -- and unconditional streaming would double-log it as a warning.

    With ``stream_stderr=True`` each stderr line is logged as it arrives *and*
    accumulated, so a long rclone pass with ``--stats=60s --stats-one-line -v``
    is observable while it runs while ``result.stderr`` still carries the whole
    output for ``rclone_transferred_bytes`` to parse. ``check=True`` still
    raises ``CalledProcessError`` with ``.stderr`` populated.
    """
    if not stream_stderr:
        return subprocess.run(cmd, check=check, text=True, capture_output=True)

    collected: list[str] = []
    stdout_chunks: list[str] = []

    with subprocess.Popen(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
    ) as process:
        # stdout is drained concurrently. rclone writes almost nothing there,
        # but a full stdout pipe would block the child while this loop waits on
        # stderr.
        def _drain_stdout() -> None:
            if process.stdout is not None:
                stdout_chunks.append(process.stdout.read())

        stdout_reader = threading.Thread(target=_drain_stdout, daemon=True)
        stdout_reader.start()

        if process.stderr is not None:
            for line in process.stderr:
                collected.append(line)
                # Progress statistics, not failures: INFO is the right level.
                log.info("%s", line.rstrip())

        returncode = process.wait()
        stdout_reader.join()

    stderr = "".join(collected)
    stdout = "".join(stdout_chunks)
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# Dataset discovery and generation identity
# --------------------------------------------------------------------------

def list_datasets(directory: Path) -> list[str] | None:
    """
    Immediate subdirectories of `directory`, sorted. One per dataset.

    Returns None if the directory exists but cannot be read. As with
    ``read_node_labels``, None means "unknown", not "empty" -- an absent or
    genuinely empty root is ``[]`` and legitimately re-applies the bootstrap
    taint, while an unreadable root must leave readiness exactly as it is. A
    denied directory can still be stat'd, so ``is_dir()`` does not catch this
    and ``iterdir()`` raises ``PermissionError``; letting that propagate exits
    the process, and treating it as empty would re-taint a node whose mirror is
    complete.
    """
    try:
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except PermissionError as exc:
        log.warning("Cannot read dataset root %s, treating its contents as unknown: %s",
                    directory, exc)
        return None


def generation_value(datasets: list[str]) -> str:
    """
    Stable short identity for the set of datasets a node holds.

    Node label values are limited to 63 characters, so this is a truncated
    digest rather than the dataset names themselves. Operators read the names
    from the pod log or directly from ``datasetRoot``; the label exists so that
    "which nodes are current" is answerable with a single kubectl query.
    """
    if not datasets:
        return "none"
    digest = hashlib.sha256("\n".join(sorted(datasets)).encode("utf-8")).hexdigest()
    return f"g{digest[:12]}"


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def rclone_transferred_bytes(output: str | None) -> int:
    """
    Bytes moved, from the last transfer-statistics line rclone writes.

    Zero when nothing matched, which is a legitimate result for an unchanged
    mirror rather than an error.
    """
    matches = _RCLONE_XFER.findall(output or "")
    if not matches:
        return 0
    amount, unit = matches[-1]
    return int(float(amount) * _UNITS.get(unit, 1))


def tree_bytes(root: Path) -> int:
    """Total size of every file under ``root`` for pass statistics."""
    total = 0
    for directory, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def sync_pass(source: Path, dest_root: Path,
              excludes: list[str] | None = None,
              transfers: int = DEFAULT_TRANSFERS,
              streams: int = DEFAULT_STREAMS) -> tuple[bool, int]:
    """
    Mirror the complete source tree directly into ``dest_root``.

    Exactly one recursive ``rclone sync <source>/ <dest_root>/`` is run. rclone
    uses its default size-and-modification-time comparison and temporary-file
    rename behavior; ``--inplace`` is never passed. Consequently replacement is
    atomic for each completed file, but the tree is not an atomic snapshot and
    completed changes are not rolled back if the command later fails.

    Returns ``(ok, completed_syncs)``. A successful rclone exit completes the
    whole pass, so ``completed_syncs`` is one on success and zero on failure.
    """
    dest_root.mkdir(parents=True, exist_ok=True)

    # Trailing separators make the command's source-root and destination-root
    # semantics explicit and avoid introducing an extra directory level.
    source_root = f"{source}{os.sep}"
    destination_root = f"{dest_root}{os.sep}"
    cmd = [
        "rclone", "sync", source_root, destination_root,
        # Preserve ownership and mode as the previous rsync -a deployment did.
        "--metadata",
        # Verifying re-reads both sides, roughly 2x cost, and the default
        # size-and-modification-time comparison is what the publication
        # procedure is required to move anyway. See ../tuning/rclone.md.
        "--ignore-checksum",
        f"--transfers={transfers}",
        f"--multi-thread-streams={streams}",
        "--multi-thread-cutoff=256M",
        "--multi-thread-chunk-size=64M",
        # The default 128 KiB is poor for this workload's scattered writes.
        "--multi-thread-write-buffer-size=4M",
        "--checkers=16",
        "--stats=60s", "--stats-one-line", "-v",
    ]

    # Split zip archives are transfer artifacts and not routing-service data.
    for pattern in excludes or []:
        cmd += ["--exclude", pattern]

    log.info("Syncing source directly to %s: %d transfers x %d streams",
             dest_root, transfers, streams)
    started = time.monotonic()
    try:
        result = run(cmd, stream_stderr=True)
    except subprocess.CalledProcessError as exc:
        elapsed = time.monotonic() - started
        # Direct mirroring has no whole-pass rollback. Completed per-file
        # replacements may already be visible; a later pass reconciles them.
        log.error("rclone mirror failed after %.1fs; completed file changes remain "
                  "and the next pass will retry: %s",
                  elapsed, (exc.stderr or "").strip()[-2000:])
        return False, 0

    elapsed = time.monotonic() - started
    transferred = rclone_transferred_bytes(result.stderr)
    mirrored = tree_bytes(dest_root)
    datasets = list_datasets(dest_root)
    # None is "unreadable", not zero. Reporting 0 here would be a lie, and
    # len(None) would take the process down after a pass that succeeded.
    inventory = ("an unreadable dataset root" if datasets is None
                 else f"{len(datasets)} dataset(s)")
    log.info("Pass complete: direct mirror has %s, %d bytes "
             "transferred, %d bytes on disk, %.1fs total",
             inventory, transferred, mirrored, elapsed)
    return True, 1


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
    log.warning("Re-applied taint %s to node %s: datasetRoot is absent or empty",
                BOOTSTRAP_TAINT, node)
    return True


def apply_unreadable_taint(node: str) -> bool:
    """
    Mark the node as holding a mirror nobody can read. --overwrite is idempotent.

    This exists because the alternative is silence. An unreadable datasetRoot
    must not rewrite the generation and count labels -- they still describe a
    complete mirror, and rewriting them to "none" would be a lie in the other
    direction -- so without a taint the node keeps advertising correct readiness
    while every consumer on it is denied. That is the failure mode this whole
    change set exists to remove, and it is exactly what the incident looked like:
    correct generation, correct count, no taint, every pod reporting ready, tree
    entirely unreadable.

    NoSchedule rather than NoExecute. Pods already running on the node are
    serving errors against a tree they cannot read, so evicting them is
    defensible, but a blocking PodDisruptionBudget plus the required node
    affinity these consumers carry can deadlock the eviction with nowhere for the
    replicas to land. Gating new pods is the safe half; draining the node is an
    operator decision, and the runbook in ../README.md covers it.
    """
    result = run(
        ["kubectl", "taint", "node", node,
         f"{UNREADABLE_TAINT}={UNREADABLE_TAINT_VALUE}:NoSchedule", "--overwrite"],
        check=False,
    )
    if result.returncode != 0:
        log.error("Could not apply taint %s to %s: %s",
                  UNREADABLE_TAINT, node, (result.stderr or "").strip())
        return False
    log.warning("Applied taint %s=%s to node %s: datasetRoot exists but cannot be "
                "read, so this node needs replacing.",
                UNREADABLE_TAINT, UNREADABLE_TAINT_VALUE, node)
    return True


def clear_unreadable_taint(node: str) -> bool:
    """
    Drop the unreadable taint once the root can be read again. Idempotent.

    Attempted on every readable pass rather than tracked in process state. A
    stuck NoSchedule taint on a healthy node silently removes it from the pool,
    which is the same class of quiet failure as the one being fixed, and process
    state cannot survive the restart that would strand it. One kubectl call per
    interval is not worth optimizing against that.
    """
    result = run(["kubectl", "taint", "node", node, f"{UNREADABLE_TAINT}-"], check=False)
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        log.info("Cleared taint %s from node %s: datasetRoot is readable again",
                 UNREADABLE_TAINT, node)
        return True
    if "not found" in stderr.lower():
        return True
    log.error("Could not clear taint %s from %s: %s", UNREADABLE_TAINT, node, stderr)
    return False


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


def reconcile_node_state(node: str, ok: bool, datasets: list[str] | None,
                         taint_removed: bool) -> bool:
    """
    Reconcile readiness with the top-level dataset names in ``datasetRoot``.

    ``datasets`` carries three outcomes and each gets its own treatment. None
    means the root could not be read, so the inventory is unknown: the generation
    and count labels are left exactly as they are, because they still describe a
    mirror that is complete on disk, and the node is marked with
    ``UNREADABLE_TAINT`` instead. An empty list means the node genuinely holds no
    datasets and legitimately re-applies the startup taint, even after a failed
    pass, so a node with no mirror cannot accept new consumers. Names present
    with a successful pass updates the generation/count labels and opens the
    startup gate; the generation is an identity of top-level names, not a
    checksum of file contents. A failed pass that still has data leaves labels
    and taint unchanged because completed per-file changes may represent only
    part of the source tree.

    ``BOOTSTRAP_TAINT`` and ``UNREADABLE_TAINT`` answer different questions and
    are never applied together. The first means "no data yet, wait"; the second
    means "data present, nobody can read it, replace the node". The clear of the
    second runs once ahead of all three readable branches, including the failed
    pass, because reaching any of them proves the root could be enumerated. That
    is what stops a transient denial from stranding a healthy node under a
    NoSchedule taint. Where the branch docstrings above say the taint is left
    unchanged, they mean ``BOOTSTRAP_TAINT``.
    """
    if datasets is None:
        log.warning("Dataset root is unreadable; leaving the generation and count "
                    "labels on node %s unchanged and marking it unreadable.", node)
        apply_unreadable_taint(node)
        return taint_removed

    clear_unreadable_taint(node)

    if not datasets:
        if apply_bootstrap_taint(node):
            taint_removed = False
        if ok:
            # A successful empty mirror must stop advertising stale data.
            write_generation_label(node, datasets)
        return taint_removed

    if not ok:
        log.warning("Pass failed with data still present; leaving node state unchanged.")
        return taint_removed

    if not taint_removed:
        taint_removed = remove_bootstrap_taint(node)
    write_generation_label(node, datasets)
    return taint_removed


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Periodic node-local dataset sync with Kubernetes taint and label management.")
    parser.add_argument("--source", required=True, help="Source directory (ONTAP mount)")
    parser.add_argument("--dest-root", required=True,
                        help="Node-local dataset root mirrored directly from the source")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between passes")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="rclone exclude pattern; repeatable. Used to drop the split "
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

    taint_removed = False

    while True:
        ok, _completed = sync_pass(source, dest_root,
                                   excludes=args.exclude,
                                   transfers=args.transfers,
                                   streams=args.streams)
        datasets = list_datasets(dest_root)

        if not args.no_node_patch:
            taint_removed = reconcile_node_state(node, ok, datasets, taint_removed)

        if args.once or _shutdown.wait(timeout=args.interval):
            break

    log.info("Stopped.")


if __name__ == "__main__":
    main()

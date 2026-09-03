"""
Unit tests for the sync agent's pure logic (deliverable 6).

No filesystem copies and no cluster. Everything here is dataset discovery,
generation identity, and link-dest candidate selection -- the parts that decide
what gets copied and what gets hardlinked.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_agent  # noqa: E402


# ---------------------------------------------------------------------------
# Dataset discovery
# ---------------------------------------------------------------------------

def test_list_datasets_returns_sorted_immediate_subdirectories(tmp_path):
    for name in ["GEODATA2026Q3", "GEODATA2026Q1", "GEODATA2026Q2"]:
        (tmp_path / name).mkdir()
    assert sync_agent.list_datasets(tmp_path) == ["GEODATA2026Q1", "GEODATA2026Q2", "GEODATA2026Q3"]


def test_list_datasets_ignores_files_and_dotfiles(tmp_path):
    """
    Only directories are datasets. On the real export, GEODATA2026Q2 sits beside
    nothing else -- but the split .zip.NNN archives live one level down and a
    future layout could put files at the top. They are not datasets.
    """
    (tmp_path / "GEODATA2026Q2").mkdir()
    (tmp_path / ".snapshot").mkdir()
    (tmp_path / "ROADNET_2026Q2_ND.zip.001").write_text("x")
    assert sync_agent.list_datasets(tmp_path) == ["GEODATA2026Q2"]


def test_list_datasets_on_missing_directory_is_empty_not_an_error(tmp_path):
    assert sync_agent.list_datasets(tmp_path / "does-not-exist") == []


def test_list_datasets_on_an_empty_readable_directory_is_empty(tmp_path):
    """
    Empty means "this node holds no datasets" and must stay distinguishable
    from unreadable, because empty legitimately re-applies the bootstrap taint.
    """
    assert sync_agent.list_datasets(tmp_path) == []


def test_list_datasets_on_an_unreadable_directory_is_none_not_empty(tmp_path):
    """
    A denied directory can still be stat'd, so `is_dir()` passes and
    `iterdir()` raises PermissionError. Unpatched that reached the top of the
    `while True:` loop and exited the process -- 269 then 379 pod restarts
    observed live. Collapsing it to [] would be worse: reconcile_node_state
    would re-taint a node whose mirror is complete.

    Induced with mode bits rather than SELinux so the case is portable.
    """
    if os.geteuid() == 0:
        pytest.skip("root bypasses mode bits, so the denial cannot be induced this way")

    denied = tmp_path / "denied"
    denied.mkdir()
    (denied / "GEODATA2026Q2").mkdir()
    os.chmod(denied, 0o000)
    try:
        assert denied.is_dir(), "the directory must remain stat-able for this to be the real case"
        assert sync_agent.list_datasets(denied) is None
    finally:
        os.chmod(denied, 0o755)


# ---------------------------------------------------------------------------
# Generation identity
# ---------------------------------------------------------------------------

def test_generation_value_is_stable_and_order_independent():
    a = sync_agent.generation_value(["GEODATA2026Q1", "GEODATA2026Q2"])
    b = sync_agent.generation_value(["GEODATA2026Q2", "GEODATA2026Q1"])
    assert a == b


def test_generation_value_differs_between_dataset_sets():
    one = sync_agent.generation_value(["GEODATA2026Q2"])
    two = sync_agent.generation_value(["GEODATA2026Q2", "GEODATA2026Q3"])
    assert one != two


def test_generation_value_empty_is_none_sentinel():
    assert sync_agent.generation_value([]) == "none"


@pytest.mark.parametrize("count", [1, 5, 50, 500])
def test_generation_value_respects_the_63_character_label_cap(count):
    """
    node label values are capped at 63 characters, which is why this is a
    digest and not the dataset names. Guard the cap directly: a regression that
    switched back to names would pass every other test here.
    """
    datasets = [f"GEODATA20{n:02d}Q1" for n in range(count)]
    assert len(sync_agent.generation_value(datasets)) <= 63


# ---------------------------------------------------------------------------
# rclone stats parsing
#
# The hardlink option and its generation parser were removed with rclone,
# which cannot hardlink against a previous generation. The tests that covered
# split_generation and pick_link_dest went with them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("2026/08/15 15:01:50 INFO  :  504.985 GiB / 504.985 GiB, 100%, 1.234 GiB/s, ETA 0s",
     int(504.985 * 1024 ** 3)),
    ("2026/08/15 15:01:50 INFO  :  1.5 MiB / 1.5 MiB, 100%, 0 B/s, ETA -",
     int(1.5 * 1024 ** 2)),
    ("2026/08/15 15:01:50 INFO  :  512 B / 512 B, 100%, 0 B/s, ETA -", 512),
    ("2026/08/15 15:01:50 INFO  :  2 TiB / 2 TiB, 100%, 0 B/s, ETA -", 2 * 1024 ** 4),
])
def test_rclone_transferred_bytes_handles_each_unit(line, expected):
    assert sync_agent.rclone_transferred_bytes(line) == expected


def test_rclone_transferred_bytes_takes_the_last_report():
    """
    --stats emits a line per interval for the life of the transfer. The final
    one is the total; an earlier one would under-report a long bootstrap.
    """
    output = "\n".join([
        "INFO  :  100 GiB / 504.985 GiB, 20%, 1.1 GiB/s, ETA 6m",
        "INFO  :  300 GiB / 504.985 GiB, 59%, 1.2 GiB/s, ETA 3m",
        "INFO  :  504.985 GiB / 504.985 GiB, 100%, 1.2 GiB/s, ETA 0s",
    ])
    assert sync_agent.rclone_transferred_bytes(output) == int(504.985 * 1024 ** 3)


def test_rclone_transferred_bytes_ignores_the_speed_field():
    """
    "0 B/s" sits right after the size pair and looks similar. Matching it would
    report a transfer of zero on a bootstrap that moved 505 GiB.
    """
    line = "INFO  :  300 KiB / 300 KiB, 100%, 0 B/s, ETA -"
    assert sync_agent.rclone_transferred_bytes(line) == 300 * 1024


def test_rclone_transferred_bytes_on_a_fully_resumed_pass_is_zero():
    """
    Nothing to move is a legitimate outcome, not a parse failure -- it is what
    a pass sees after the rename has already happened.
    """
    assert sync_agent.rclone_transferred_bytes("") == 0
    assert sync_agent.rclone_transferred_bytes("no stats here") == 0
    assert sync_agent.rclone_transferred_bytes(None) == 0


def test_tree_bytes_sums_every_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.geodatabase").write_bytes(b"x" * 1000)
    (tmp_path / "sub" / "b.dat").write_bytes(b"y" * 337)
    assert sync_agent.tree_bytes(tmp_path) == 1337


def test_tree_bytes_of_an_empty_tree_is_zero(tmp_path):
    assert sync_agent.tree_bytes(tmp_path) == 0


# ---------------------------------------------------------------------------
# Generation labelling
# ---------------------------------------------------------------------------
class _FakeRun:
    """
    Stands in for sync_agent.run, recording kubectl invocations.

    `labels` is the node's current label set as the fake `kubectl get` reports
    it; None makes the read fail, which is the "cannot tell" path.
    """

    def __init__(self, labels: dict | None, taint_rc: int = 0):
        self.labels = labels
        self.taint_rc = taint_rc
        self.commands: list[list[str]] = []

    def __call__(self, cmd, check=True, *, stream_stderr=False):
        self.commands.append(cmd)
        if cmd[:3] == ["kubectl", "get", "node"]:
            if self.labels is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(self.labels), stderr="")
        if cmd[:2] == ["kubectl", "taint"]:
            stderr = "denied" if self.taint_rc else ""
            return subprocess.CompletedProcess(cmd, self.taint_rc, stdout="", stderr=stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    @property
    def writes(self):
        return [c for c in self.commands if c[:2] == ["kubectl", "label"]]

    @property
    def taints(self):
        return [c for c in self.commands if c[:2] == ["kubectl", "taint"]]


def _labels_for(datasets):
    return {
        sync_agent.GENERATION_LABEL: sync_agent.generation_value(datasets),
        sync_agent.DATASET_COUNT_LABEL: str(len(datasets)),
    }


def test_unchanged_labels_are_not_rewritten(monkeypatch):
    """
    The point of the amendment: in steady state the agent runs a pass every
    interval for the life of the node and must not write on every one.
    """
    datasets = ["GEODATA2026Q2"]
    fake = _FakeRun(_labels_for(datasets))
    monkeypatch.setattr(sync_agent, "run", fake)

    assert sync_agent.write_generation_label("node-a", datasets) is True
    assert fake.writes == [], "unchanged labels must not trigger a write"


def test_repeated_passes_write_nothing_after_the_first(monkeypatch):
    datasets = ["GEODATA2026Q2"]
    fake = _FakeRun({})  # node starts unlabelled
    monkeypatch.setattr(sync_agent, "run", fake)

    sync_agent.write_generation_label("node-a", datasets)
    assert len(fake.writes) == 1

    fake.labels = _labels_for(datasets)  # first write landed
    for _ in range(10):
        sync_agent.write_generation_label("node-a", datasets)
    assert len(fake.writes) == 1, "steady state must stay at a single write"


def test_a_new_dataset_does_trigger_a_write(monkeypatch):
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    sync_agent.write_generation_label("node-a", ["GEODATA2026Q2", "GEODATA2026Q3"])

    assert len(fake.writes) == 1
    written = " ".join(fake.writes[0])
    assert sync_agent.generation_value(["GEODATA2026Q2", "GEODATA2026Q3"]) in written
    assert f"{sync_agent.DATASET_COUNT_LABEL}=2" in written


def test_externally_removed_labels_are_restored(monkeypatch):
    """
    Self-healing, which the unconditional write gave for free and a naive
    in-memory cache would have lost. This label is what publish-time
    checks read, so drift must not persist.
    """
    datasets = ["GEODATA2026Q2"]
    fake = _FakeRun({})  # someone wiped the labels
    monkeypatch.setattr(sync_agent, "run", fake)

    sync_agent.write_generation_label("node-a", datasets)
    assert len(fake.writes) == 1


def test_a_drifted_count_label_alone_triggers_a_write(monkeypatch):
    labels = _labels_for(["GEODATA2026Q2"])
    labels[sync_agent.DATASET_COUNT_LABEL] = "99"
    fake = _FakeRun(labels)
    monkeypatch.setattr(sync_agent, "run", fake)

    sync_agent.write_generation_label("node-a", ["GEODATA2026Q2"])
    assert len(fake.writes) == 1


def test_an_unreadable_node_falls_back_to_writing(monkeypatch):
    """A failed read means 'unknown', not 'already correct'."""
    fake = _FakeRun(None)
    monkeypatch.setattr(sync_agent, "run", fake)

    assert sync_agent.write_generation_label("node-a", ["GEODATA2026Q2"]) is True
    assert len(fake.writes) == 1


# ---------------------------------------------------------------------------
# Node state reconciliation
#
# The dataset lives on instance-store NVMe, which is lost on a stop or a
# hardware replacement. Karpenter's startupTaints fire at registration only, so
# the agent is the only thing that can re-close the gate on a node whose data
# vanished under it.
# ---------------------------------------------------------------------------

_APPLY = f"{sync_agent.BOOTSTRAP_TAINT}=true:NoSchedule"
_REMOVE = f"{sync_agent.BOOTSTRAP_TAINT}-"


def test_successful_direct_sync_derives_labels_from_dataset_root_and_removes_taint(
    tmp_path, monkeypatch,
):
    """A successful pass publishes readiness from the direct mirror's top level."""
    source = tmp_path / "source"
    dataset_root = tmp_path / "dataset-root"
    source.mkdir()
    dataset_root.mkdir()
    expected_datasets = ["GEODATA2026Q1", "GEODATA2026Q3"]
    for name in reversed(expected_datasets):
        (source / name).mkdir()
        mirrored_dataset = dataset_root / name
        mirrored_dataset.mkdir()
        (mirrored_dataset / "network.geodatabase").write_text(name)
    (dataset_root / "mirror-metadata.txt").write_text("not a dataset")

    fake = _FakeRun({})
    monkeypatch.setattr(sync_agent, "run", fake)

    ok, completed = sync_agent.sync_pass(source, dataset_root)
    datasets = sync_agent.list_datasets(dataset_root)
    taint_removed = sync_agent.reconcile_node_state(
        "node-a", ok, datasets, taint_removed=False,
    )

    assert (ok, completed) == (True, 1)
    assert datasets == expected_datasets
    assert taint_removed is True
    assert [command[4] for command in fake.taints] == [_REMOVE]
    assert len(fake.writes) == 1
    written = " ".join(fake.writes[0])
    assert (
        f"{sync_agent.GENERATION_LABEL}="
        f"{sync_agent.generation_value(expected_datasets)}"
    ) in written
    assert f"{sync_agent.DATASET_COUNT_LABEL}=2" in written


@pytest.mark.parametrize(
    "dataset_root_exists",
    [False, True],
    ids=["absent-dataset-root", "empty-dataset-root"],
)
def test_absent_or_empty_dataset_root_reasserts_the_bootstrap_taint(
    tmp_path, monkeypatch, dataset_root_exists,
):
    dataset_root = tmp_path / "dataset-root"
    if dataset_root_exists:
        dataset_root.mkdir()

    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    datasets = sync_agent.list_datasets(dataset_root)
    taint_removed = sync_agent.reconcile_node_state(
        "node-a", True, datasets, taint_removed=True,
    )

    assert datasets == []
    assert taint_removed is False, "the gate must close so it can reopen after a resync"
    assert [command[4] for command in fake.taints] == [_APPLY]
    assert "--overwrite" in fake.taints[0], "re-tainting must be idempotent"


def test_data_present_never_reasserts_the_taint(monkeypatch):
    """
    Steady state is a pass every interval for the life of the node. None of
    them may touch the taint once it is off.
    """
    datasets = ["GEODATA2026Q2"]
    fake = _FakeRun(_labels_for(datasets))
    monkeypatch.setattr(sync_agent, "run", fake)

    for _ in range(10):
        sync_agent.reconcile_node_state("node-a", True, datasets, taint_removed=True)

    assert fake.taints == []


def test_the_taint_is_removed_again_after_data_returns(monkeypatch):
    """
    Why the state is tracked rather than the taint written every pass: the gate
    has to reopen once the agent has re-synced, or the node never comes back.
    """
    fake = _FakeRun({})
    monkeypatch.setattr(sync_agent, "run", fake)

    removed = sync_agent.reconcile_node_state("node-a", True, ["GEODATA2026Q2"], taint_removed=False)
    assert removed is True

    removed = sync_agent.reconcile_node_state("node-a", True, [], taint_removed=removed)
    assert removed is False

    removed = sync_agent.reconcile_node_state("node-a", True, ["GEODATA2026Q2"], taint_removed=removed)
    assert removed is True

    assert [c[4] for c in fake.taints] == [_REMOVE, _APPLY, _REMOVE]


def test_empty_active_resets_the_generation_labels(monkeypatch):
    """
    A node that lost its data must stop advertising a generation it no longer
    holds, and that label is what publish-time checks read.
    """
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    sync_agent.reconcile_node_state("node-a", True, [], taint_removed=True)

    assert len(fake.writes) == 1
    written = " ".join(fake.writes[0])
    assert f"{sync_agent.GENERATION_LABEL}=none" in written
    assert f"{sync_agent.DATASET_COUNT_LABEL}=0" in written


def test_a_failed_pass_with_an_empty_root_reapplies_the_taint(monkeypatch):
    """
    A failed pass may leave completed file changes visible, but an absent or
    empty datasetRoot must reapply the startup taint even when the pass failed.
    Label updates still wait for a successful pass.
    """
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    taint_removed = sync_agent.reconcile_node_state("node-a", False, [], taint_removed=True)

    assert taint_removed is False
    assert [call[4] for call in fake.taints] == [_APPLY]
    assert fake.writes == []


def test_a_rejected_taint_write_does_not_reopen_the_gate(monkeypatch):
    """
    If the re-taint did not land, the taint is still absent -- so the agent must
    not record it as removed and skip the retry on the next empty pass.
    """
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]), taint_rc=1)
    monkeypatch.setattr(sync_agent, "run", fake)

    taint_removed = sync_agent.reconcile_node_state("node-a", True, [], taint_removed=True)

    assert taint_removed is True
    taint_removed = sync_agent.reconcile_node_state("node-a", True, [], taint_removed=taint_removed)
    assert [c[4] for c in fake.taints] == [_APPLY, _APPLY], "a failed apply must be retried"


def test_an_unreadable_dataset_root_leaves_the_taint_and_labels_untouched(monkeypatch):
    """
    None from list_datasets means "unknown", so node state is left exactly as
    it is: no taint applied, no taint removed, no label written.

    This is the hazard that mattered more than the crash. A node whose mirror is
    complete but unreadable must not be tainted and must not have its generation
    label rewritten to "none" -- that destroys correct state, and the label is
    what publish-time checks read.
    """
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    taint_removed = sync_agent.reconcile_node_state(
        "node-a", True, None, taint_removed=True,
    )

    assert taint_removed is True, "an unknown inventory must not change the recorded gate state"
    assert fake.taints == [], "unknown must neither apply nor remove the taint"
    assert fake.writes == [], "unknown must not rewrite the generation or count labels"
    assert fake.commands == [], "unknown must not touch the node object at all"


def test_an_unreadable_root_does_not_reopen_a_closed_gate(monkeypatch):
    """The pass-through is symmetric: a closed gate stays closed too."""
    fake = _FakeRun({})
    monkeypatch.setattr(sync_agent, "run", fake)

    assert sync_agent.reconcile_node_state(
        "node-a", False, None, taint_removed=False,
    ) is False
    assert fake.commands == []


def test_an_empty_dataset_root_still_reapplies_the_taint_unlike_an_unreadable_one(monkeypatch):
    """
    The pair that proves the two outcomes are genuinely distinct. Same node,
    same starting state: [] closes the gate and rewrites the labels, None does
    nothing.
    """
    empty = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", empty)
    assert sync_agent.reconcile_node_state("node-a", True, [], taint_removed=True) is False
    assert [command[4] for command in empty.taints] == [_APPLY]
    assert len(empty.writes) == 1

    unreadable = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", unreadable)
    assert sync_agent.reconcile_node_state("node-a", True, None, taint_removed=True) is True
    assert unreadable.taints == []
    assert unreadable.writes == []


# ---------------------------------------------------------------------------
# Streamed stderr (F7)
#
# --stats=60s --stats-one-line -v is worthless if it is buffered until rclone
# exits: the 58.9-minute first sync on the replacement node logged nothing after
# its startup banner. The streaming lives behind `run` rather than in a separate
# function because every test in this suite intercepts rclone by patching
# sync_agent.run, and argv must not change.
# ---------------------------------------------------------------------------

def _emitter(lines: list[str], exit_code: int = 0) -> list[str]:
    """A command that writes `lines` to stderr, flushing each, then exits."""
    script = (
        "import sys\n"
        f"for line in {lines!r}:\n"
        "    sys.stderr.write(line + '\\n')\n"
        "    sys.stderr.flush()\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", script]


def test_streamed_stderr_reaches_the_log_before_the_process_exits(tmp_path, caplog):
    """
    Asserting only that the lines were logged by the time `run` returned would
    also pass on the buffered implementation, which logs nothing until rclone
    exits. So the child waits for proof that its first line was already logged
    before it writes its last one: the interleaving is the behaviour under test.
    """
    handshake = tmp_path / "first-line-was-logged"
    first = "INFO  :  100 GiB / 504.985 GiB, 20%, 1.1 GiB/s, ETA 6m"
    last = "INFO  :  504.985 GiB / 504.985 GiB, 100%, 1.2 GiB/s, ETA 0s"

    class _Handshake(logging.Handler):
        def emit(self, record):
            if first in record.getMessage():
                handshake.touch()

    script = (
        "import pathlib, sys, time\n"
        f"sys.stderr.write({first!r} + '\\n')\n"
        "sys.stderr.flush()\n"
        f"handshake = pathlib.Path({str(handshake)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not handshake.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.stderr.write(('observed' if handshake.exists() else 'BUFFERED') + '\\n')\n"
        f"sys.stderr.write({last!r} + '\\n')\n"
    )

    handler = _Handshake()
    sync_agent.log.addHandler(handler)
    try:
        with caplog.at_level("INFO", logger="sync-agent"):
            result = sync_agent.run([sys.executable, "-c", script], stream_stderr=True)
    finally:
        sync_agent.log.removeHandler(handler)

    assert "observed" in result.stderr, (
        "the first stderr line must reach the log while the process is still running"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert result.returncode == 0
    for line in (first, last):
        assert line in logged, "every stats line must be logged as the pass runs"
        assert line in result.stderr, "the accumulated stderr must stay complete"


def test_streamed_stderr_is_still_parsed_for_the_last_statistics_line():
    """
    rclone_transferred_bytes matches the LAST statistics line, so accumulation
    order matters as much as completeness.
    """
    lines = [
        "INFO  :  100 GiB / 504.985 GiB, 20%, 1.1 GiB/s, ETA 6m",
        "INFO  :  504.985 GiB / 504.985 GiB, 100%, 1.2 GiB/s, ETA 0s",
    ]

    result = sync_agent.run(_emitter(lines), stream_stderr=True)

    assert sync_agent.rclone_transferred_bytes(result.stderr) == int(504.985 * 1024 ** 3)


def test_streamed_stderr_still_raises_with_stderr_populated_on_failure():
    """
    sync_pass's handler reads (exc.stderr or "").strip()[-2000:], so a streamed
    failure must carry the same payload a buffered one did.
    """
    lines = ["ERROR : lstat /mnt/geodata/GEODATA2026Q2: permission denied"]

    with pytest.raises(subprocess.CalledProcessError) as raised:
        sync_agent.run(_emitter(lines, exit_code=3), stream_stderr=True)

    assert raised.value.returncode == 3
    assert lines[0] in raised.value.stderr


def test_streaming_is_opt_in_so_kubectl_calls_are_unchanged():
    """
    Default False keeps the kubectl callers byte-for-byte as they were. They log
    stderr selectively -- remove_bootstrap_taint treats "not found" as a normal
    info-level outcome -- and unconditional streaming would double-log it and
    turn that into a spurious warning.
    """
    lines = ["not found"]

    result = sync_agent.run(_emitter(lines, exit_code=1), check=False)

    assert result.returncode == 1
    assert "not found" in result.stderr

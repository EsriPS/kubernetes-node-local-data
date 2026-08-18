"""
Unit tests for the sync agent's pure logic (deliverable 6).

No filesystem copies and no cluster. Everything here is dataset discovery,
generation identity, and link-dest candidate selection -- the parts that decide
what gets copied and what gets hardlinked.
"""

import json
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

    def __call__(self, cmd, check=True):
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


def test_empty_active_reasserts_the_bootstrap_taint(monkeypatch):
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    taint_removed = sync_agent.reconcile_node_state("node-a", True, [], taint_removed=True)

    assert taint_removed is False, "the gate must close so it can reopen after a resync"
    assert [c[4] for c in fake.taints] == [_APPLY]
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


def test_a_failed_pass_leaves_node_state_untouched(monkeypatch):
    """
    A failed pass may just be an incomplete copy. Only a CLEAN pass that still
    finds active/ empty means the data is genuinely gone -- re-tainting on a
    transient rsync failure would flap the gate on every source hiccup.
    """
    fake = _FakeRun(_labels_for(["GEODATA2026Q2"]))
    monkeypatch.setattr(sync_agent, "run", fake)

    taint_removed = sync_agent.reconcile_node_state("node-a", False, [], taint_removed=True)

    assert taint_removed is True
    assert fake.taints == []
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

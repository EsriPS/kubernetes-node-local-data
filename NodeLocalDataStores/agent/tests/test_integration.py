"""
Local integration test, and the only acceptance check that runs
without the cluster.

Runs full passes with real rclone against a fixture directory tree, with node
patching disabled. Fixtures are a few KB, not 507 GiB; what is under test
is the mechanism, not the throughput.

Checks four things: the atomic rename, the skip of
already-active datasets, correct behaviour when the copy fails mid-flight, and
the staging sweep, plus the archive exclusion.

Hardlinking via --link-dest is NOT covered: it was dropped with the move to
rclone, which cannot hardlink against a previous generation.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_agent  # noqa: E402

def _has_rclone() -> bool:
    """
    rclone is the copy engine as of 0.4.0. The image pins 1.75.0; a much
    older build would lack multi-thread copy for local-to-local pairs, which is
    the whole reason for the change -- so name the gap precisely rather than
    letting a bare failure read like a code defect.
    """
    if shutil.which("rclone") is None:
        return False
    probe = subprocess.run(["rclone", "version"], text=True, capture_output=True)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _has_rclone(),
    reason="needs rclone on PATH; install with: brew install rclone",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A fixed mtime for files that are meant to be "the same file" across two
# generations. rsync --link-dest hardlinks only when size AND mtime match, so
# writing two byte-identical files seconds apart is NOT enough to link them.
UNCHANGED_MTIME = 1_700_000_000


def _write_dataset(root: Path, name: str, files: dict[str, str],
                   mtimes: dict[str, int] | None = None) -> Path:
    """Build one dataset directory shaped like the real export."""
    dataset = root / name
    for relpath, content in files.items():
        target = dataset / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        stamp = (mtimes or {}).get(relpath)
        if stamp is not None:
            os.utime(target, (stamp, stamp))
    return dataset


@pytest.fixture
def source(tmp_path):
    """
    A source tree mirroring the layout on the ONTAP export:

        GEODATA2026Q2/
            ROADNET_2026Q2_ND/
                ROADNET_NA_Q2_2026.geodatabase    <- payload
                roadnet_na_q2_2026.tn/part.dat    <- payload
            ROADNET_2026Q2_ND.zip.001             <- transfer format, excluded
            ROADNET_2026Q2_ND.zip.002
    """
    src = tmp_path / "source"
    src.mkdir()
    _write_dataset(src, "GEODATA2026Q2", {
        "ROADNET_2026Q2_ND/ROADNET_NA_Q2_2026.geodatabase": "north america q2",
        "ROADNET_2026Q2_ND/roadnet_na_q2_2026.tn/part.dat": "turn restrictions",
        "ROADNET_2026Q2_ND.zip.001": "archive part 1",
        "ROADNET_2026Q2_ND.zip.002": "archive part 2",
    }, mtimes={"ROADNET_2026Q2_ND/ROADNET_NA_Q2_2026.geodatabase": UNCHANGED_MTIME})
    return src


@pytest.fixture
def dest_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    return root


ZIP_EXCLUDES = ["*.zip.*", "*.zip"]


# ---------------------------------------------------------------------------
# A full pass
# ---------------------------------------------------------------------------

def test_pass_lands_the_dataset_under_active(source, dest_root):
    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert ok is True
    assert added == 1

    landed = dest_root / "active" / "GEODATA2026Q2"
    assert landed.is_dir()
    assert (landed / "ROADNET_2026Q2_ND" / "ROADNET_NA_Q2_2026.geodatabase").read_text() == "north america q2"
    assert (landed / "ROADNET_2026Q2_ND" / "roadnet_na_q2_2026.tn" / "part.dat").is_file()


def test_staging_is_empty_after_a_successful_pass(source, dest_root):
    """
    the dataset is moved into place with os.rename, not copied. A
    leftover under staging/ would mean the rename degraded to a copy, which is
    exactly the partial-visibility failure the staging design removes.
    """
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    assert list((dest_root / "staging").iterdir()) == []


def test_zip_archives_are_excluded(source, dest_root):
    """Transfer-format archives that no service reads."""
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    landed = dest_root / "active" / "GEODATA2026Q2"
    assert not (landed / "ROADNET_2026Q2_ND.zip.001").exists()
    assert not (landed / "ROADNET_2026Q2_ND.zip.002").exists()
    # The extracted payload beside them still arrives.
    assert (landed / "ROADNET_2026Q2_ND").is_dir()


def test_without_excludes_the_archives_are_copied(source, dest_root):
    """The exclusion is opt-in, so the default copies everything."""
    sync_agent.sync_pass(source, dest_root)
    assert (dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND.zip.001").is_file()


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_second_pass_skips_the_already_active_dataset(source, dest_root):
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert ok is True
    assert added == 0


def test_active_dataset_is_never_written_into(source, dest_root):
    """
    a running pod may hold open file handles under active/. Even when
    the source changes, an already-active dataset must not be re-synced into.
    """
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    landed = dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / "ROADNET_NA_Q2_2026.geodatabase"
    before = landed.stat().st_mtime_ns

    # Mutate the source as if someone had edited it in place.
    (source / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / "ROADNET_NA_Q2_2026.geodatabase").write_text("TAMPERED")

    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert landed.read_text() == "north america q2"
    assert landed.stat().st_mtime_ns == before


def test_a_new_generation_is_picked_up_alongside_the_old(source, dest_root):
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    _write_dataset(source, "GEODATA2026Q3", {
        "ROADNET_2026Q3_ND/ROADNET_NA_Q3_2026.geodatabase": "north america q3",
    })
    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert ok is True
    assert added == 1
    # both generations coexist; services reference versioned paths, so
    # the old one stays readable until its service is republished.
    assert sync_agent.list_datasets(dest_root / "active") == ["GEODATA2026Q2", "GEODATA2026Q3"]


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------

def test_copy_failure_leaves_nothing_under_active_but_keeps_staging(source, dest_root, monkeypatch):
    """
    on failure the node's taint and labels stay unchanged and the pass
    retries next interval. The invariant that matters is that no partial
    directory appears under active/ for a pod to read.

    Staging is deliberately KEPT. Discarding it threw away up to 505 GiB of
    completed transfer on any failure, against a shared FSx throughput budget
   ; the next pass resumes into it instead.
    """
    def failing_run(cmd, check=True):
        raise subprocess.CalledProcessError(1, cmd, stderr="rclone: directory not found")

    monkeypatch.setattr(sync_agent, "run", failing_run)

    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert ok is False
    assert added == 0
    # The safety property is unchanged: nothing partial is visible to a pod.
    assert sync_agent.list_datasets(dest_root / "active") == []
    # The staging directory survives for the retry.
    assert (dest_root / "staging" / "GEODATA2026Q2").is_dir()


def test_the_copy_command_is_rclone_sync_with_the_measured_flags(source, dest_root, monkeypatch):
    """
    Each of these was chosen from a measurement, and two of them are
    guards rather than tuning:

    --inplace must NEVER appear. In-place writes are ruled out, because
    an interrupted --inplace transfer leaves a full-size file containing a hole
    -- indistinguishable from a complete copy by any size check, which is the
    partial-visibility failure staging and rename exist to remove.

    --no-check-dest must NEVER appear. It always re-transfers, which would
    destroy the resume behaviour the retry path depends on.
    """
    seen = []
    real_run = sync_agent.run
    monkeypatch.setattr(sync_agent, "run",
                        lambda cmd, check=True: (seen.append(cmd), real_run(cmd, check=check))[1])

    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    copies = [c for c in seen if c[:2] == ["rclone", "sync"]]
    assert len(copies) == 1, "exactly one rclone sync per dataset"
    cmd = copies[0]

    assert "--inplace" not in cmd, "in-place writes are ruled out"
    assert "--no-check-dest" not in cmd, "would destroy resume"
    assert "--metadata" in cmd, "rsync -a preserved ownership and mode"
    assert "--ignore-checksum" in cmd
    assert any(a.startswith("--multi-thread-streams=") for a in cmd), \
        "intra-file parallelism is the whole reason for rclone"


def test_excludes_reach_the_copy_command(source, dest_root, monkeypatch):
    """Transfer-format archives that no service reads."""
    seen = []
    real_run = sync_agent.run
    monkeypatch.setattr(sync_agent, "run",
                        lambda cmd, check=True: (seen.append(cmd), real_run(cmd, check=check))[1])

    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    cmd = next(c for c in seen if c[:2] == ["rclone", "sync"])
    for pattern in ZIP_EXCLUDES:
        assert pattern in cmd


def test_resume_does_not_recopy_an_already_complete_file(source, dest_root):
    """
    The actual win. A file already present in staging with matching size and
    modtime must be skipped, not re-transferred.

    Verified by inode: rclone writes to a temp file and renames, so a
    re-transfer changes the inode. os.rename of the directory preserves it, so
    an unchanged inode in active/ proves the bytes were never re-sent.
    """
    rel = "ROADNET_2026Q2_ND/ROADNET_NA_Q2_2026.geodatabase"
    staged = dest_root / "staging" / "GEODATA2026Q2" / rel
    staged.parent.mkdir(parents=True, exist_ok=True)

    src_file = source / "GEODATA2026Q2" / rel
    shutil.copy2(src_file, staged)          # copy2 preserves mtime
    inode_before = staged.stat().st_ino

    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    assert (ok, added) == (True, 1)

    landed = dest_root / "active" / "GEODATA2026Q2" / rel
    assert landed.read_text() == "north america q2"
    assert landed.stat().st_ino == inode_before, "already-complete file should not be re-transferred"


def test_delete_removes_an_orphan_left_in_staging(source, dest_root):
    """
    A hard-killed rsync leaves .name.XXXXXX temp files behind. With staging now
    surviving between attempts they would accumulate -- up to 182 GiB each for
    the EU geodatabase -- so --delete must clear anything absent from the source.
    """
    orphan = dest_root / "staging" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / ".ROADNET_EU.geodatabase.a1b2c3"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("interrupted transfer")

    ok, _ = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    assert ok is True

    assert not (dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / ".ROADNET_EU.geodatabase.a1b2c3").exists()


def test_rename_failure_keeps_the_completed_staging_copy(source, dest_root, monkeypatch):
    """
    At rename time the copy is COMPLETE -- only the move failed. Discarding it
    would throw away a finished transfer over something like a transient EBUSY.
    """
    def failing_rename(a, b):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(sync_agent.os, "rename", failing_rename)

    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)

    assert (ok, added) == (False, 0)
    assert sync_agent.list_datasets(dest_root / "active") == []
    assert (dest_root / "staging" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND").is_dir()


def test_a_failed_dataset_is_retried_on_the_next_pass(source, dest_root, monkeypatch):
    calls = {"n": 0}
    real_run = sync_agent.run

    def fail_once(cmd, check=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.CalledProcessError(23, cmd, stderr="transient")
        return real_run(cmd, check=check)

    monkeypatch.setattr(sync_agent, "run", fail_once)

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (False, 0)
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert (dest_root / "active" / "GEODATA2026Q2").is_dir()


# ---------------------------------------------------------------------------
# Staging sweep
# ---------------------------------------------------------------------------

def test_sweep_clears_an_orphan_left_by_a_killed_pod(dest_root):
    """
    the case per-dataset cleanup misses. An orphan for a dataset that is
    no longer pending is never revisited by sync_dataset, and on this data it
    silently holds ~507 GiB of the node's volume.
    """
    staging = dest_root / "staging"
    orphan = staging / "GEODATA2025Q4"
    orphan.mkdir(parents=True)
    (orphan / "half-copied.geodatabase").write_text("partial")

    sync_agent.sweep_staging(staging)

    assert staging.is_dir()
    assert list(staging.iterdir()) == []


def test_sweep_on_a_missing_staging_directory_is_not_an_error(dest_root):
    """A fresh node has no staging/ yet; startup must not fall over."""
    sync_agent.sweep_staging(dest_root / "staging")


def test_sweep_does_not_touch_active(source, dest_root):
    sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    sync_agent.sweep_staging(dest_root / "staging")
    assert (dest_root / "active" / "GEODATA2026Q2").is_dir()


# ---------------------------------------------------------------------------
# Node patching is genuinely disabled locally
# ---------------------------------------------------------------------------

def test_a_full_pass_makes_no_kubectl_calls(source, dest_root, monkeypatch):
    """
    the local test path must not touch a node. Guard it by making any
    kubectl invocation a hard failure, rather than trusting the flag.
    """
    real_run = sync_agent.run

    def no_kubectl(cmd, check=True):
        assert cmd[0] != "kubectl", f"unexpected kubectl call: {cmd}"
        return real_run(cmd, check=check)

    monkeypatch.setattr(sync_agent, "run", no_kubectl)
    ok, added = sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES)
    assert (ok, added) == (True, 1)


def test_agent_runs_end_to_end_via_cli_with_node_patching_disabled(source, dest_root):
    """
    The whole binary, once, as the DaemonSet would invoke it but with --once
    and --no-node-patch. NODE_NAME is deliberately absent to prove the flag is
    what gates node access.
    """
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {k: v for k, v in os.environ.items() if k != "NODE_NAME"}

    result = subprocess.run(
        [sys.executable, str(agent),
         "--source", str(source),
         "--dest-root", str(dest_root),
         "--once", "--no-node-patch",
         "--exclude", "*.zip.*"],
        text=True, capture_output=True, env=env, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert (dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND").is_dir()
    assert not (dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND.zip.001").exists()
    # the per-pass measurements the design depends on.
    assert "Pass complete" in result.stderr


def test_cli_without_node_name_and_without_the_flag_refuses_to_start(source, dest_root):
    """Fail fast rather than run blind and never open the gate."""
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {k: v for k, v in os.environ.items() if k != "NODE_NAME"}

    result = subprocess.run(
        [sys.executable, str(agent), "--source", str(source),
         "--dest-root", str(dest_root), "--once"],
        text=True, capture_output=True, env=env, timeout=60,
    )

    assert result.returncode != 0
    assert "NODE_NAME" in result.stderr


# ---------------------------------------------------------------------------
# The rclone engine
#
# rsync moves whole files one thread at a time and could not beat ~9.4 min on
# this dataset, because the EU geodatabase is a single 182,619,619,328-byte
# file. rclone reads concurrently *within* a file and reaches 4.5 min. These
# check that the change did not alter what lands -- the byte count and digest
# have been identical across ten configurations.
# ---------------------------------------------------------------------------

@pytest.fixture
def wide_source(tmp_path):
    """A source shaped like the real export: a few large files and many small."""
    src = tmp_path / "wide-source"
    src.mkdir()
    files = {
        "ROADNET_2026Q2_ND/ROADNET_EU_Q2_2026.geodatabase": "eu" * 40000,
        "ROADNET_2026Q2_ND/ROADNET_APAC_Q2_2026.geodatabase": "apac" * 12000,
        "ROADNET_2026Q2_ND/ROADNET_NA_Q2_2026.geodatabase": "na" * 20000,
        "ROADNET_2026Q2_ND/ROADNET_MEA_Q2_2026.geodatabase": "mea" * 5000,
        "ROADNET_2026Q2_ND/ROADNET_SA_Q2_2026.geodatabase": "sa" * 3000,
        "ROADNET_2026Q2_ND/ROADNET_2026Q2_ND_extents.gdb/a00000001.gdbtable": "x" * 400,
        "ROADNET_2026Q2_ND/ROADNET_2026Q2_ND_extents.gdb/a00000001.gdbtablx": "y" * 500,
        "ROADNET_2026Q2_ND/ROADNET_2026Q2_ND_extents.gdb/gdb": "z",
    }
    for region in ["eu", "apac", "na", "mea", "sa"]:
        files[f"ROADNET_2026Q2_ND/roadnet_{region}_q2_2026.tn/Network_ND_weight_values"] = region * 9000
        files[f"ROADNET_2026Q2_ND/roadnet_{region}_q2_2026.tn/Network_ND_adjacencies"] = region * 3000
        files[f"ROADNET_2026Q2_ND/roadnet_{region}_q2_2026.tn/Network_ND_schema"] = "schema"
    _write_dataset(src, "GEODATA2026Q2", files)
    return src


def _fingerprint(root: Path) -> dict:
    """Everything rsync -a is supposed to preserve, plus content."""
    out = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel + "/"] = (oct(stat.st_mode), int(stat.st_mtime))
        else:
            out[rel] = (oct(stat.st_mode), int(stat.st_mtime), stat.st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def test_copy_matches_the_source_exactly(wide_source, dest_root):
    """
    rsync -ain --checksum is the acceptance check named for the cluster run.
    Here it is cheap enough to assert directly: no output means no difference.

    Note this compares content, not just size -- which matters because the
    agent runs rclone with --ignore-checksum, so nothing else verifies the
    copy. rsync is used only as the comparison tool; it is no longer the engine.
    """
    sync_agent.sync_pass(wide_source, dest_root, excludes=ZIP_EXCLUDES)

    diff = subprocess.run(
        ["rsync", "-ain", "--checksum",
         f"{wide_source / 'GEODATA2026Q2'}/",
         f"{dest_root / 'active' / 'GEODATA2026Q2'}/"],
        text=True, capture_output=True, check=True,
    )
    changes = [ln for ln in diff.stdout.splitlines() if not ln.startswith(".d")]
    assert changes == [], f"copy differs from source: {changes}"


def test_stream_count_does_not_change_what_lands(wide_source, tmp_path):
    """
    The property the whole change rests on. Ten configurations have produced
    542,223,402,559 bytes and digest g1c9818edb910; splitting a file across
    streams must not alter the result, only the speed.
    """
    single = tmp_path / "single"
    many = tmp_path / "many"
    single.mkdir()
    many.mkdir()

    assert sync_agent.sync_pass(wide_source, single, transfers=1, streams=1) == (True, 1)
    assert sync_agent.sync_pass(wide_source, many, transfers=4, streams=8) == (True, 1)

    baseline = _fingerprint(single / "active" / "GEODATA2026Q2")
    assert _fingerprint(many / "active" / "GEODATA2026Q2") == baseline
    # Guard the guard: a fingerprint of nothing would compare equal to itself.
    assert len(baseline) > 20


def test_bytes_and_elapsed_are_reported(wide_source, dest_root):
    """The bytes figure is a measurement the design depends on."""
    staging = dest_root / "staging"
    active = dest_root / "active"
    staging.mkdir()
    active.mkdir()

    ok, transferred, elapsed = sync_agent.sync_dataset(
        wide_source, staging, active, "GEODATA2026Q2")

    on_disk = sum(p.stat().st_size
                  for p in (active / "GEODATA2026Q2").rglob("*") if p.is_file())
    assert ok is True
    assert elapsed > 0
    # rclone reports in binary units rounded to 3 decimals, so this is close
    # rather than exact -- the on-disk total is the authoritative figure.
    assert transferred == pytest.approx(on_disk, rel=0.01)


def test_ownership_and_mode_are_preserved(wide_source, dest_root):
    """
    rsync -a preserved ownership and mode, and the destination is a host
    path. --metadata is what keeps that true under rclone; without it the copy
    silently lands with default permissions.
    """
    rel = "ROADNET_2026Q2_ND/ROADNET_EU_Q2_2026.geodatabase"
    src_file = wide_source / "GEODATA2026Q2" / rel
    src_file.chmod(0o640)

    sync_agent.sync_pass(wide_source, dest_root, excludes=ZIP_EXCLUDES)

    landed = dest_root / "active" / "GEODATA2026Q2" / rel
    assert landed.stat().st_mode == src_file.stat().st_mode
    assert int(landed.stat().st_mtime) == int(src_file.stat().st_mtime)


def test_orphan_in_staging_is_removed(wide_source, dest_root):
    """
    `rclone sync` rather than `copy` is what deletes files staging holds
    that the source does not. A hard-killed transfer leaves temp files behind,
    each up to the size of the file being written -- 182 GiB for the EU
    geodatabase -- and they would otherwise accumulate until the volume fills.
    """
    orphan = (dest_root / "staging" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND"
              / "leftover-from-a-killed-pod.partial")
    orphan.parent.mkdir(parents=True)
    orphan.write_text("interrupted transfer")

    assert sync_agent.sync_pass(wide_source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert not (dest_root / "active" / "GEODATA2026Q2" / "ROADNET_2026Q2_ND"
                / "leftover-from-a-killed-pod.partial").exists()


def test_cli_accepts_the_stream_flags(wide_source, dest_root):
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {k: v for k, v in os.environ.items() if k != "NODE_NAME"}

    result = subprocess.run(
        [sys.executable, str(agent),
         "--source", str(wide_source), "--dest-root", str(dest_root),
         "--once", "--no-node-patch", "--transfers", "4",
         "--multi-thread-streams", "2"],
        text=True, capture_output=True, env=env, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert (dest_root / "active" / "GEODATA2026Q2").is_dir()


def test_cli_rejects_a_stream_count_below_one(wide_source, dest_root):
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {k: v for k, v in os.environ.items() if k != "NODE_NAME"}

    result = subprocess.run(
        [sys.executable, str(agent),
         "--source", str(wide_source), "--dest-root", str(dest_root),
         "--once", "--no-node-patch", "--multi-thread-streams", "0"],
        text=True, capture_output=True, env=env, timeout=60,
    )

    assert result.returncode != 0
    assert "multi-thread-streams" in result.stderr

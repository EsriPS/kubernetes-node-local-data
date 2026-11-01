"""Real-rclone integration tests for the direct node-local mirror.

Every scenario in this module requires exactly rclone 1.75.0, matching the
supported agent image. The fixtures are deliberately small: these tests verify
source-root mirror semantics, comparison behavior, failure/retry behavior, and
command construction rather than throughput.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_agent  # noqa: E402


REQUIRED_RCLONE_VERSION = "1.75.0"
ZIP_EXCLUDES = ["*.zip.*", "*.zip"]
FIXED_MTIME_NS = 1_700_000_000_000_000_000


@pytest.fixture(scope="session", autouse=True)
def require_exact_rclone_version():
    """Fail integration validation unless the image-pinned rclone is present."""
    executable = shutil.which("rclone")
    if executable is None:
        pytest.fail(
            "agent integration tests require executable rclone 1.75.0 on PATH; "
            "install or provide that exact version before running validation",
            pytrace=False,
        )

    probe = subprocess.run(
        [executable, "version"], text=True, capture_output=True, check=False,
    )
    first_line = (probe.stdout or "").splitlines()[:1]
    detected = first_line[0].strip() if first_line else "no version reported"
    expected = f"rclone v{REQUIRED_RCLONE_VERSION}"
    if probe.returncode != 0 or detected != expected:
        detail = (probe.stderr or "").strip()
        pytest.fail(
            f"agent integration tests require exactly {expected}; detected "
            f"{detected!r}" + (f" ({detail})" if detail else ""),
            pytrace=False,
        )


# ---------------------------------------------------------------------------
# Fixtures and assertions
# ---------------------------------------------------------------------------


def _write_dataset(
    root: Path,
    name: str,
    files: dict[str, str],
    *,
    mtime_ns: int | None = None,
) -> Path:
    """Build one top-level dataset directory shaped like the ONTAP export."""
    dataset = root / name
    for relpath, content in files.items():
        target = dataset / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if mtime_ns is not None:
            os.utime(target, ns=(mtime_ns, mtime_ns))
    return dataset


def _file_tree(root: Path) -> dict[str, bytes]:
    """Return all mirrored file paths and bytes, independent of metadata."""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _included_source_tree(source: Path) -> dict[str, bytes]:
    return {
        path: content
        for path, content in _file_tree(source).items()
        if ".zip." not in path and not path.endswith(".zip")
    }


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_dataset(
        source_root,
        "GEODATA2026Q2",
        {
            "ROADNET_2026Q2_ND/ROADNET_NA_Q2_2026.geodatabase": "north america q2",
            "ROADNET_2026Q2_ND/roadnet_na_q2_2026.tn/part.dat": "turn restrictions",
            "ROADNET_2026Q2_ND.zip.001": "archive part 1",
            "ROADNET_2026Q2_ND.zip.002": "archive part 2",
        },
        mtime_ns=FIXED_MTIME_NS,
    )
    return source_root


@pytest.fixture
def dest_root(tmp_path: Path) -> Path:
    destination = tmp_path / "dataset-root"
    destination.mkdir()
    return destination


# ---------------------------------------------------------------------------
# Direct-mirror command and required real-rclone scenarios
# ---------------------------------------------------------------------------


def test_pass_runs_exactly_one_recursive_source_root_sync_without_inplace(
    source: Path, dest_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    seen: list[list[str]] = []
    real_run = sync_agent.run

    def recording_run(cmd: list[str], check: bool = True, *, stream_stderr: bool = False):
        seen.append(cmd)
        return real_run(cmd, check=check, stream_stderr=stream_stderr)

    monkeypatch.setattr(sync_agent, "run", recording_run)

    assert sync_agent.sync_pass(
        source, dest_root, excludes=ZIP_EXCLUDES, transfers=3, streams=2,
    ) == (True, 1)

    expected = [
        "rclone", "sync", f"{source}{os.sep}", f"{dest_root}{os.sep}",
        "--metadata",
        "--ignore-checksum",
        "--transfers=3",
        "--multi-thread-streams=2",
        "--multi-thread-cutoff=256M",
        "--multi-thread-chunk-size=64M",
        "--multi-thread-write-buffer-size=4M",
        "--checkers=16",
        "--stats=60s", "--stats-one-line", "-v",
        "--exclude", "*.zip.*",
        "--exclude", "*.zip",
    ]
    sync_commands = [cmd for cmd in seen if cmd[:2] == ["rclone", "sync"]]
    assert sync_commands == [expected]
    assert "--inplace" not in sync_commands[0]
    assert "--checksum" not in sync_commands[0]
    assert "--ignore-times" not in sync_commands[0]


def test_real_rclone_adds_source_paths_directly_to_dataset_root(
    source: Path, dest_root: Path,
):
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)

    _write_dataset(
        source,
        "GEODATA2026Q3",
        {"ROADNET_2026Q3_ND/ROADNET_NA_Q3_2026.geodatabase": "north america q3"},
    )
    added_file = source / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / "new-index.dat"
    added_file.write_text("new index")

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert _file_tree(dest_root) == _included_source_tree(source)
    assert (dest_root / "GEODATA2026Q2").is_dir()
    assert (dest_root / "GEODATA2026Q3").is_dir()
    assert not (dest_root / "active").exists()
    assert not (dest_root / "staging").exists()


def test_real_rclone_updates_source_modifications_by_size(
    source: Path, dest_root: Path,
):
    target = source / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / "ROADNET_NA_Q2_2026.geodatabase"
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)

    original_mtime_ns = target.stat().st_mtime_ns
    target.write_text("north america q2 revised with a different size")
    os.utime(target, ns=(original_mtime_ns, original_mtime_ns))

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    mirrored = dest_root / target.relative_to(source)
    assert mirrored.read_text() == "north america q2 revised with a different size"
    assert _file_tree(dest_root) == _included_source_tree(source)


def test_real_rclone_propagates_source_deletions_and_removes_destination_orphans(
    source: Path, dest_root: Path,
):
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)

    deleted_source = (
        source / "GEODATA2026Q2" / "ROADNET_2026Q2_ND"
        / "roadnet_na_q2_2026.tn" / "part.dat"
    )
    deleted_destination = dest_root / deleted_source.relative_to(source)
    deleted_source.unlink()
    orphan = dest_root / "GEODATA2025Q4" / "orphan.geodatabase"
    orphan.parent.mkdir()
    orphan.write_text("destination-only content")

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert not deleted_destination.exists()
    assert not orphan.exists()
    assert _file_tree(dest_root) == _included_source_tree(source)


def test_real_rclone_honors_configured_excludes(source: Path, dest_root: Path):
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)

    assert _file_tree(dest_root) == _included_source_tree(source)
    assert not (dest_root / "GEODATA2026Q2" / "ROADNET_2026Q2_ND.zip.001").exists()
    assert not (dest_root / "GEODATA2026Q2" / "ROADNET_2026Q2_ND.zip.002").exists()
    assert (dest_root / "GEODATA2026Q2" / "ROADNET_2026Q2_ND").is_dir()


def test_real_rclone_interrupted_pass_is_reconciled_by_successful_retry(
    source: Path,
    dest_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    large_file = source / "GEODATA2026Q2" / "ROADNET_2026Q2_ND" / "large.geodatabase"
    large_file.write_bytes(b"direct-mirror-data" * 524_288)  # 9 MiB, long enough to interrupt.

    real_run = sync_agent.run
    calls = 0

    def interrupt_once(cmd: list[str], check: bool = True, *, stream_stderr: bool = False):
        nonlocal calls
        calls += 1
        if calls != 1:
            return real_run(cmd, check=check, stream_stderr=stream_stderr)

        process = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Signal the real process immediately: local-to-local copies can finish
        # even multi-megabyte fixtures before a sleep-based interrupter runs.
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        if process.returncode == 0:
            pytest.fail("the throttled real-rclone pass completed before it could be interrupted")
        raise subprocess.CalledProcessError(
            process.returncode, cmd, output=stdout, stderr=stderr,
        )

    monkeypatch.setattr(sync_agent, "run", interrupt_once)

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (False, 0)
    # A failed direct pass has no rollback contract: completed files may remain.
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert calls == 2
    assert _file_tree(dest_root) == _included_source_tree(source)


# ---------------------------------------------------------------------------
# Default rclone comparison semantics
# ---------------------------------------------------------------------------


def test_real_rclone_uses_default_size_and_modification_time_detection(
    source: Path, dest_root: Path,
):
    relative = Path("GEODATA2026Q2/ROADNET_2026Q2_ND/comparison.dat")
    source_file = source / relative
    source_file.write_text("AAAA")
    os.utime(source_file, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))

    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    destination_file = dest_root / relative
    assert destination_file.read_text() == "AAAA"

    # Same path, size, and mtime is intentionally considered unchanged without
    # checksum mode, even if a publisher silently changed the bytes.
    source_file.write_text("BBBB")
    os.utime(source_file, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert destination_file.read_text() == "AAAA"

    # Once modification time changes, the same-sized replacement is detected.
    changed_mtime_ns = FIXED_MTIME_NS + 2_000_000_000
    os.utime(source_file, ns=(changed_mtime_ns, changed_mtime_ns))
    assert sync_agent.sync_pass(source, dest_root, excludes=ZIP_EXCLUDES) == (True, 1)
    assert destination_file.read_text() == "BBBB"
    assert _file_tree(dest_root) == _included_source_tree(source)


def test_real_rclone_without_excludes_copies_all_source_content(
    source: Path, dest_root: Path,
):
    assert sync_agent.sync_pass(source, dest_root) == (True, 1)
    assert _file_tree(dest_root) == _file_tree(source)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_agent_runs_end_to_end_via_cli_with_node_patching_disabled(
    source: Path, dest_root: Path,
):
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {key: value for key, value in os.environ.items() if key != "NODE_NAME"}

    result = subprocess.run(
        [
            sys.executable,
            str(agent),
            "--source", str(source),
            "--dest-root", str(dest_root),
            "--once",
            "--no-node-patch",
            "--exclude", "*.zip.*",
            "--exclude", "*.zip",
            "--transfers", "4",
            "--multi-thread-streams", "2",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert _file_tree(dest_root) == _included_source_tree(source)
    assert not (dest_root / "active").exists()
    assert "Pass complete: direct mirror" in result.stderr


def test_cli_without_node_name_and_without_the_flag_refuses_to_start(
    source: Path, dest_root: Path,
):
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {key: value for key, value in os.environ.items() if key != "NODE_NAME"}

    result = subprocess.run(
        [
            sys.executable,
            str(agent),
            "--source", str(source),
            "--dest-root", str(dest_root),
            "--once",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )

    assert result.returncode != 0
    assert "NODE_NAME" in result.stderr


def test_cli_rejects_a_stream_count_below_one(source: Path, dest_root: Path):
    agent = Path(__file__).resolve().parent.parent / "sync_agent.py"
    env = {key: value for key, value in os.environ.items() if key != "NODE_NAME"}

    result = subprocess.run(
        [
            sys.executable,
            str(agent),
            "--source", str(source),
            "--dest-root", str(dest_root),
            "--once",
            "--no-node-patch",
            "--multi-thread-streams", "0",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )

    assert result.returncode != 0
    assert "multi-thread-streams" in result.stderr

# Tuning the sync engine

Use a sync engine that reads concurrently within a single file. That one
property is the difference between a roughly twenty-minute sync and a roughly
five-minute one on this shape of data.

Figures come from a test cluster: a roughly 500 GB dataset, read from FSx for
NetApp ONTAP onto local NVMe, on nodes with 48 vCPU. Treat them as indicative.
ONTAP is the authoritative source. The agent runs exactly one recursive
`rclone sync <source>/ <datasetRoot>/`, where `datasetRoot` is the exact
configured destination (`/mnt/geodata` by default). Source additions, changes,
and deletions are reflected in the mirror, subject to configured excludes.

## Why a whole-file copier cannot reach the target

| Engine | Two-node sync | Per node |
|---|---|---|
| Single rsync | ~20 min | ~400 MB/s |
| rsync, 3 parallel workers | ~10 min | ~870 MB/s |
| rclone, 8 transfers x 8 streams | **~4.5 min** | **~1,900 MB/s** |

rsync moves whole files, one thread at a time. Running several rsync processes
splits the work by file, which helps until the largest file dominates. In a
typical dataset here the largest member is a single geodatabase of well over a
hundred gigabytes, and no amount of file-level parallelism divides it.

rclone issues concurrent ranged reads within one file, so a single large file is
split across streams. That is the property to look for in any alternative
engine.

## The settings

```
--transfers 8 --multi-thread-streams 8 --metadata
```

`--transfers` is file-level concurrency and `--multi-thread-streams` is
intra-file concurrency. The product is the number of operations in flight, which
is what determines both throughput and memory.

These two figures were measured rather than chosen. At 8 x 8 the cold read
reached about 2,000 MB/s and saturated provisioned IOPS at roughly 110%, so
raising them further has nothing left to claim. Lowering them costs throughput
directly.

## Comparison and deletion semantics

Rclone normally decides whether a destination file is current by comparing its
size and modification time with the source. Publishing a same-named file on
ONTAP must change its size or modification time for the agent to detect it
without checksum mode. After a successful pass, every included path in
`datasetRoot` matches by that comparison, and paths absent from ONTAP have been
deleted. Excluded paths are outside that guarantee.

This makes ONTAP the gold copy. Node-local content is not retained independently
of its source. Confirm source deletions deliberately before publishing them: the
next successful sync removes the corresponding mirror paths from every node.

## Flags that carry correctness

**`--metadata` is required.** Rclone's local backend preserves modification time
only by default. Without this flag, ownership and mode are dropped. Nothing else
catches it: the byte total matches, and a content digest matches, because only
the permissions differ. It is the least visible setting and the easiest to lose
during benchmarking.

**`--inplace` must never be set.** With multi-thread streams the destination can
be extended to its full size before all data arrives, so an interrupted in-place
transfer can leave a final path with its expected size but incomplete contents.
Without `--inplace`, rclone writes changed content to a temporary file and
renames it over the destination only after transfer completes.

That rename gives atomic replacement of one final file path, not atomicity for
the whole directory tree. Additions, replacements, and deletions become visible
at different times during a pass, so consumers can observe a mix of old and new
files. If a dataset release requires cross-file atomicity, coordinate ONTAP
publication with draining or restarting consumers; the sync agent does not
provide a snapshot switch.

**`--no-check-dest` must never be set.** It forces unnecessary retransfers on
every pass and defeats the normal size-and-modification-time comparison that
makes routine retries and incremental syncs affordable.

The agent's tests assert the absence of the last two flags. Both were introduced
during benchmarking, and one survived into a committed deployment definition
before it was caught. A throughput flag can quietly contradict a settled
correctness decision, so assert on it rather than relying on review.

## Space and interrupted passes

The agent maintains only the direct mirror in exact `datasetRoot`; it does not
maintain a second full mirror tree. Size local NVMe for the complete source
mirror, rclone temporary files for files currently transferring or being
replaced, and operational headroom for growth and workload activity. The
temporary requirement depends on transfer concurrency and the sizes of files
changing in the same pass; it is not another complete dataset copy.

A failed or interrupted pass is not rolled back. Files already added, replaced,
or deleted can remain visible, while final paths for incomplete changed-file
transfers retain their old content because `--inplace` is absent. The agent
retries on its next interval. A later successful pass reconciles the direct
mirror, including transfer temporary files left by interruption.

## Memory

Allocate 1Gi as both request and limit. Peak anonymous memory during a cold sync
is roughly 560 MiB at 8 x 8, sampled from inside the cgroup, so 1Gi is about
1.8x headroom.

512Mi does not survive. The process is OOMKilled within seconds. A smaller limit
measured against a single-threaded rsync agent does not transfer, because rclone
holds transfers x streams x chunk size in flight.

If the limit has to come down, the lever is concurrency, and lowering it costs
throughput. Re-measure rather than reason about buffer arithmetic; the
arithmetic suggests a ceiling several times higher than the observed peak.

CPU can be requested at 10m. With no CPU limit set, the request is a scheduling
weight rather than a cap, and the agent still uses about a core on an idle node.
A cold sync at 10m and at 4 cores finished within a second of each other. Where
a low request costs something is a routine update rather than the first sync,
since a re-sync competes with service pods that have real requests.

## Readiness labels and verification

After every successful pass, the dataset-generation label hashes the sorted
top-level names directly in `datasetRoot`, and dataset-count records their
count. The generation is an identity for the top-level dataset set, not a
checksum of file contents. Changing files beneath unchanged top-level names does
not change the label.

Check all of the following after a sync:

- included file counts, byte totals, and expected source deletions agree with the
  direct mirror in exact `datasetRoot`;
- mode and modification time match the source, proving `--metadata` took effect;
- no second dataset tree exists, and local free space still includes temporary
  transfer capacity and operational headroom;
- the node's dataset-generation and dataset-count labels are set and the startup
  taint is cleared.

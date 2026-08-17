# Tuning the copy engine

Use a copy engine that reads concurrently within a single file. That one
property is the difference between a roughly twenty-minute sync and a roughly
five-minute one on this shape of data.

Figures come from a test cluster: a roughly 500 GB dataset, read from FSx for
NetApp ONTAP onto local NVMe, on nodes with 48 vCPU. Treat them as indicative.

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

## Three flags carry correctness, not performance

**`--metadata` is required.** rclone's local backend preserves modification time
only. Without this flag, ownership and mode are dropped. Nothing else catches
it: the byte total matches, and a content digest matches, because only the
permissions differ. It is the least visible of the three and the easiest to lose
during benchmarking.

**`--inplace` must never be set.** With multi-thread streams the destination is
extended to its full size before the data arrives, so an interrupted in-place
transfer leaves a file at its final name and final size containing holes. No
size check distinguishes that from a complete copy.

**`--no-check-dest` must never be set.** It re-transfers everything on every
pass. That satisfies the letter of a retry requirement while destroying the
property that makes retry affordable, which is resuming into a staging directory
that survived the failure.

The agent's tests assert the absence of the last two. Both were introduced
during benchmarking, and one survived into a committed manifest before it was
caught. A flag chosen for throughput is exactly the kind of change that quietly
contradicts a settled decision, so assert on it rather than relying on review.

## Memory

Allocate 1Gi as both request and limit. Peak anonymous memory during a cold sync
is roughly 560 MiB at 8 x 8, sampled from inside the cgroup, so 1Gi is about
1.8x headroom.

512Mi does not survive. The copy is OOMKilled within seconds, and a kill
mid-sync leaves the node without data and its services unable to start. A
smaller limit measured against a single-threaded rsync agent does not transfer,
because rclone holds transfers x streams x chunk size in flight.

If the limit has to come down, the lever is concurrency, and lowering it costs
throughput. Re-measure rather than reason about buffer arithmetic; the
arithmetic suggests a ceiling several times higher than the observed peak.

CPU can be requested at 10m. With no CPU limit set, the request is a scheduling
weight rather than a cap, and the agent still uses about a core on an idle node.
A cold sync at 10m and at 4 cores finished within a second of each other. Where
a low request costs something is a routine update rather than the first sync,
since a re-sync competes with service pods that have real requests.

## Verify the copy, not just the timing

Check all four after a sync:

- byte count and file count match the source
- mode and modification time match the source, which is what proves `--metadata`
  took effect
- the staging directory is empty, which proves the rename stayed a rename
- the node's generation label is set and the bootstrap taint is cleared

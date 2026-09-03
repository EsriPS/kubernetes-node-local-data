# Measured results

What the sample achieved, on what, and what you should re-measure for your own
environment. Numbers are rounded. They come from one test cluster and are
indicative rather than a specification.

## Test conditions

| | |
|---|---|
| Dataset | ~500 GB in ~100 files, largest member a single geodatabase of well over 100 GB |
| Source | FSx for NetApp ONTAP, single-AZ, NFS v4.1 |
| Nodes | 2 x `r8id.12xlarge`, 48 vCPU, ~2.6 TB local NVMe, Bottlerocket |
| Destination | Instance-store NVMe, bound at `/mnt/geodata` |
| Copy engine | rclone 1.75.0, 8 transfers x 8 multi-thread streams |

Every result below was taken cold. Page caches were dropped on both nodes
beforehand, and filesystem-side disk reads were confirmed to account for
essentially all bytes moved. A cache-warm run of the same work looks
considerably faster and predicts nothing about a new node.

## Sync time

| Configuration | Two nodes, concurrent | Per node |
|---|---|---|
| Single-threaded copy | ~20 min | ~400 MB/s |
| 3 parallel workers, file-level only | ~10 min | ~870 MB/s |
| 8 transfers x 8 streams | **~4.5 min** | **~1,900 MB/s** |

Two nodes reading the same dataset finished in the same time as one node alone.
The filesystem coalesced the two read streams rather than dividing its capacity
between them. That holds only for nodes reading identical data.

### The conditions behind the ~4.5 min figure

The headline number is a benchmark result, not a service level. It was taken on a
dedicated test cluster with the source filesystem provisioned at 16,000 IOPS and
serving nothing else — no live query workload, no other node syncing, no
concurrent publication — against ~500 GB in ~100 files whose size distribution
was dominated by one very large geodatabase that parallelises well across
multi-thread streams. Both nodes were reading identical data, page caches had been
dropped, and the run was started by hand rather than landing on a node that was
also being brought into service. Change any of those and the figure moves.

### A counter-measurement from a real first sync

The same configuration, on a replacement node brought up in the live environment:

```
Pass complete: ... 901830389268 bytes transferred, 901830740761 bytes on disk, 3533.9s total
```

That is **58.9 minutes for ~840 GiB, about 255 MB/s** — roughly a seventh of the
benchmark rate. The differences were the conditions, not the software:

- the FSx source was concurrently serving live reads from a second node, so the
  read-stream coalescing that made two test nodes free did not apply;
- the payload was dominated by 18–35 GB locator archives rather than one very
  large geodatabase, so per-file concurrency carried more of the work than
  multi-thread streams within a file; and
- the node was being brought into service at the same time, alongside ordinary
  cluster activity.

**Both numbers are real.** Neither supersedes the other, and the difference
between them is entirely the conditions each was taken under. Plan node
replacement and first-sync windows against the hour-scale figure, not against the
benchmark: the table above says roughly 500 GB in about five minutes, and that is
what the hardware can do when nothing else is competing for the source, not what a
first sync will take on a busy filesystem.

## What set the ceiling, in order of discovery

**Not the network, though with less room to spare than the other two.** The
instance sustains 22.5 Gbps, and the best result of about 1,900 MB/s is 15.2
Gbps of that, so roughly two thirds used with about a third still in reserve.
Bandwidth was the wrong place to look because of flow count rather than
headroom: a single TCP connection is capped near 5 Gbps, about 625 MB/s, by an
AWS per-flow limit, so no individual reader could get near the link on its own.
`nconnect=16` lifted five parallel readers from about 600 MB/s to about 1,400
MB/s. What settles it is the IOPS table below, where throughput tracked
provisioned IOPS while link headroom remained.

**Not the destination disk.** Local NVMe changed the sync time by less than
10%. It matters for the read path the services use, which is the reason to
adopt it, and not for how fast the first copy completes.

**Not the filesystem's throughput setting.** Doubling provisioned throughput
moved the result about 2%.

**Provisioned IOPS.** At roughly 120 KB per disk operation on a cold read,
provisioned IOPS translates directly into a throughput ceiling.

| Provisioned IOPS | Cold read | Time for ~500 GB |
|---|---|---|
| 10,000 | ~1,200 MB/s | ~7.5 min |
| 16,000 | ~2,000 MB/s | ~4.5 min |

Disk IOPS ran at about 110% of provisioned in both cases, which is what confirms
IOPS was binding.

**And the copy engine.** A whole-file copier cannot use any of the above on a
dataset dominated by one very large file, whatever the storage is doing.

## Resource footprint

| | |
|---|---|
| Peak agent memory | ~560 MiB, sampled inside the cgroup during a cold sync |
| Configured | 1Gi request and limit |
| CPU | 10m request, no limit; about one core used in practice |

512Mi is not survivable at these concurrency settings. Memory scales with
transfers x streams.

## What to re-measure

- **Your IOPS ceiling.** The table above is the model that mattered most, and it
  depends on your average operation size.
- **Cold versus warm.** Confirm your measurement is cold before drawing any
  conclusion from it.
- **Memory, if you change concurrency.** The buffer arithmetic suggests a
  ceiling several times higher than the observed peak, so reason from a
  measurement rather than from the arithmetic.
- **Read performance of the services themselves.** This document measures
  staging time. The reason to stage data locally is the read path, which depends
  on your services and your query mix.

## Historical raw-manifest validation

The raw configuration formerly maintained under `manifests/` was the tested
configuration, with names and environment-specific values replaced by
placeholders. It was validated by rendering those placeholders and running a
server-side dry run; the renamed set was not itself run end to end. That
validation is historical: it no longer validates the maintained deployment
source and does not establish the status of the migrated implementation.

## Current implementation validation status

The Helm chart and documented example values are now the maintained deployment
source, and the sync agent now mirrors the ONTAP source directly into
`datasetRoot`. Their validation is tracked separately through the current chart
render checks, direct-mirror sync-agent tests, required real-rclone integration
scenarios, strict Helm lint, and placeholder check. The performance measurements
in this document were collected only under the historical test conditions above;
they have not been repeated as qualification of the Helm chart or direct-mirror
sync-agent implementation and must be re-measured in each target environment.

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

## What set the ceiling, in order of discovery

**Not the network.** The instance offers 22.5 Gbps and the sync used about a
fifth of it. A single TCP connection is capped near 5 Gbps by an AWS per-flow
limit, so the constraint was flow count rather than bandwidth. `nconnect=16`
lifted five parallel readers from about 600 MB/s to about 1,400 MB/s.

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

## Validation status of these manifests

The configuration in `manifests/` is the tested configuration with names and
environment-specific values replaced by placeholders. It was validated by
rendering the placeholders and running a server-side dry run. The renamed set
has not itself been run end to end.

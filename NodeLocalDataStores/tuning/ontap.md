# Tuning the NFS source

Size the filesystem for IOPS, not for throughput. On a cold read the sync is
bound by provisioned IOPS, and raising the throughput setting instead buys
almost nothing.

All figures come from a test cluster: a roughly 500 GB dataset on FSx for NetApp
ONTAP, read by two nodes with local NVMe. Treat them as indicative and size
against your own data.

## Provisioned IOPS sets the cold-read ceiling

A cold read moves about 120 KB per disk operation. That ratio turns provisioned
IOPS directly into a throughput ceiling:

| Provisioned IOPS | Cold read achieved | Time for ~500 GB |
|---|---|---|
| 10,000 | ~1,200 MB/s | ~7.5 min |
| 16,000 | ~2,000 MB/s | ~4.5 min |

Both rows were measured, and the second met a five-minute target. Disk IOPS ran
at about 110% of provisioned in each case, which is what confirms IOPS was the
binding resource rather than throughput or the network.

Raising throughput capacity while IOPS stayed where it was produced a change of
roughly 2%. If a sync is slower than you expect, look at the IOPS metric before
changing anything else.

## Verify the read is cold before you trust any number

A warm page cache makes every measurement meaningless, and it does so silently.
On a cache-warm run the filesystem's disk-read metric sits at zero while
protocol reads look healthy, so the sync appears fast for reasons that will not
repeat on a new node.

Before a measurement, drop the page cache on the reading nodes and confirm from
the filesystem side that disk reads are non-zero and account for essentially all
of the bytes copied. On the run behind the table above, disk reads accounted for
1084.1 GB of the 1084.4 GB copied across two nodes.

## nconnect multiplies concurrency that already exists

A single TCP connection to the filesystem is subject to an AWS per-flow limit of
about 5 Gbps, regardless of instance size or enhanced networking. Five parallel
readers on one connection divided that limit five ways and returned nearly
identical per-stream rates, which is the signature of one resource being shared
rather than five independent readers.

Setting `nconnect=16` on the mount took the same five readers from about
600 MB/s to about 1,400 MB/s, a factor of 2.3. The instance already had 22.5
Gbps available and was using roughly a fifth of it. The lever was flow count.

Two conditions have to hold for this to help:

- **The reader must be concurrent.** A single-threaded copier issues one read at
  a time, so fifteen of sixteen connections sit idle. `nconnect` and a
  parallel-capable copy engine are one change, not two.
- **The filesystem must have IOPS headroom.** `nconnect` was tested earlier
  while disk IOPS utilization sat at 120–150% and measured as no improvement at
  all. A connection limit cannot show while a disk limit is binding, so that
  null result carried no information. Raise IOPS first, then test the mount
  options.

That sequencing error is the most transferable lesson here. A null result means
something only once the resource you are testing is the one the workload is
waiting on.

## Two nodes reading the same data do not divide the filesystem

The intuition that two concurrent syncs each get half the filesystem is wrong
when they read the same bytes. On the measured run, one node finished in about
4.5 minutes and two concurrent nodes finished in about 4.5 minutes each. The
filesystem coalesced the two read streams, roughly doubling bytes per disk
operation without additional disk IOPS.

This holds only for nodes reading identical data, which is exactly the case when
a pool of nodes stages the same dataset. Nodes reading different datasets do
divide the filesystem.

## What to check, in order

1. Is the read cold? Disk-read metrics non-zero, accounting for the bytes moved.
2. Is disk IOPS utilization near or above 100%? If so, provisioned IOPS is the
   ceiling and nothing else will move the number.
3. Is the copy engine issuing concurrent reads? If not, mount options will not
   help. See [rclone.md](rclone.md).
4. Only then consider `nconnect`, and re-measure.

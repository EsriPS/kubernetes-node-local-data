# Traps

Things that cost time on the way to a working deployment. Each is stated as a
rule with the reason behind it.

## Registering data stores

**Registering a hostPath folder data store affects the whole organisation.**
ArcGIS adds a hostPath volume for that path to service Deployments
organisation-wide, not only to the services that read the data. On a test
cluster that was 21 pods, including map, caching, sync, geometry, printing and
symbol services. Every node that may run any ArcGIS service pod must therefore
have the path present. That is what the path shim DaemonSet is for, and it must
be applied and Ready before you register.

**`hostPath.type: Directory` does not create a missing path.** It produces a
hard `MountVolume.SetUp` failure and the pod never starts. This is the loud
failure you want on the consumption side, and it is why the shim uses
`DirectoryOrCreate` for the parent while ArcGIS uses `Directory`.

**A hostPath folder data store cannot be registered from Enterprise Manager.**
The UI offers PV-based registration only. Use the Admin REST API, and pass
`allowServicesRestart` because the call restarts service pods.

**"Does not exist within the data store" usually means mis-placement, not a bad
path.** If the publishing tools run on nodes that carry an empty copy of the
path, the mount succeeds and the data is absent, which reports as though the
path were wrong. Give the publishing tools the same placement as the service
that reads the data.

**Clearing placement for a rebuild must be undone afterwards.** Publishing stays
broken until the tools are re-pinned, and the symptom is the error above rather
than anything that points at placement.

**Changing a local volume's path is a delete and recreate, in order.** A
volume's source is immutable and ArcGIS owns the claim bound to it. Recreating
the volume while that claim exists leaves it Released with a stale claim
reference, which never rebinds and reports only as a binding timeout.
Deregister the data store first, confirm the claim is gone, then replace the
volume and re-register.

**Verify what republishing does to placement.** If republishing a service resets
its node affinity and tolerations, every data update needs manual intervention
to re-place the pods. Test this once on your version before planning around it.

## Karpenter

**Automated node image rotation must be disabled for nodes holding staged
data.** Drift replacement discards the local copy and re-runs the sync on a
schedule set by the OS release cadence. The same guidance applies to ArcGIS
workloads generally; see [../../Guides/Karpenter/README.md](../../Guides/Karpenter/README.md).

**A NodePool has no minimum size.** Karpenter launches nodes only for pending
pods, and DaemonSets do not count because they schedule onto nodes that already
exist. Use low-priority placeholder pods with required anti-affinity on
hostname.

**Placeholder pods do not create nodes when equivalent capacity is already
schedulable.** Applying them while an older pool carrying the same label is
still up simply schedules them there. Cordon the old nodes and then delete the
placeholders so they reschedule. Cordoning alone is not enough, because
already-running pods stay where they are.

**Do not cordon every node of the pool at once.** With nothing schedulable,
Karpenter provisions an additional node. Cordon one, move its workload, uncordon
it.

**A blocking PodDisruptionBudget plus required node affinity can deadlock a node
replacement.** Evicted pods cannot reschedule anywhere, the replica never
becomes Ready, allowed disruptions drop to zero, and the drain hangs
indefinitely.

**Soft pod anti-affinity is silently undone by bin-packing.** Replicas that
prefer to spread can end up on one node with nothing reporting a problem. Use
required anti-affinity where the spread matters.

**Pinning the pool to one zone is correct when the source filesystem is
single-AZ, and wrong when it is not.** Reading across an availability zone costs
transfer charges and latency on every byte of every sync. Check the filesystem's
deployment type before copying a zone pin from an example.

## Measurement

**Cache-warm numbers do not represent a first sync.** Confirm disk reads are
non-zero on the filesystem side before trusting any throughput figure. See
[ontap.md](ontap.md).

**A null result means nothing while a different resource is saturated.** Testing
a connection limit while disk IOPS sits above 100% is guaranteed to measure no
improvement, and that result carries no information about connections. Establish
which resource is binding first.

**Change one thing.** Throughput, IOPS and cache state moving together produced
several confident and wrong conclusions on the way to the numbers in these
notes.

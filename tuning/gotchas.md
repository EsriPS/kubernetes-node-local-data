# Traps

Things that cost time on the way to a working deployment. Each is stated as a
rule with the reason behind it.

## Publishing and mirroring data

**ONTAP is authoritative, and exact `datasetRoot` is a direct mirror.** The
agent runs exactly one recursive `rclone sync <source>/ <datasetRoot>/`.
Included source additions and changes are copied, and paths deleted from ONTAP
are deleted from every node mirror, subject to configured excludes. Node-local
edits that must survive do not belong in the mirror.

**A same-named source update must change size or modification time.** Rclone's
default comparison does not enable checksums. If an ONTAP publication preserves
both values, the agent can regard different contents as current. Ensure the
publication procedure changes at least one of them, then verify the successful
pass.

**Per-file replacement is not a whole-tree transaction.** The agent never uses
`--inplace`. Rclone transfers changed content to a temporary file and renames it
over the final path after that file completes, so a consumer does not open a
partially transferred final file. Additions, replacements, and deletions still
become visible at different times during the pass. Drain or restart consumers as
part of source publication when a release requires cross-file atomicity.

**An interrupted pass does not roll back completed work.** Files already added,
replaced, or deleted can remain visible. The agent retries on the next interval,
and a later successful pass reconciles the mirror and transfer temporary files.
Size instance storage for the complete source mirror, temporary files for
in-flight transfers and replacements, and operational headroom; there is no
second full mirror tree.

**The dataset-generation label is not a content checksum.** It hashes sorted
top-level names in exact `datasetRoot`, while dataset-count reports their count.
Changing contents beneath the same top-level names does not change either
signal. Use agent logs and successful-pass timing to observe those updates.

## Registering data stores

**Registering a hostPath folder data store affects the whole organisation.**
ArcGIS adds a hostPath volume for exact `datasetRoot` to service Deployments
organisation-wide, not only to the services that read the data. On a test
cluster that was 21 pods, including map, caching, sync, geometry, printing and
symbol services. Every node that may run any ArcGIS service pod must therefore
have the path present. That is what the Path Shim DaemonSet is for, and it must
be Ready before you register.

**`hostPath.type: Directory` does not create a missing path.** It produces a
hard `MountVolume.SetUp` failure and the pod never starts. This is the loud
failure you want on the consumption side, and it is why the shim uses
`DirectoryOrCreate` for exact `datasetRoot` while ArcGIS uses `Directory`.

**A hostPath folder data store cannot be registered from Enterprise Manager.**
The UI offers PV-based registration only. Use the Admin REST API, and pass
`allowServicesRestart` because the call restarts service pods.

**"Does not exist within the data store" usually means mis-placement, not a bad
path.** If the publishing tools run on nodes that carry an empty copy of exact
`datasetRoot`, the mount succeeds and the data is absent, which reports as
though the path were wrong. Give the publishing tools the same placement as the
service that reads the data.

**Clearing placement for a rebuild must be undone afterwards.** Publishing stays
broken until the tools are re-pinned, and the symptom is the error above rather
than anything that points at placement.

**Changing a local volume's path is a delete and recreate, in order.** A
volume's source is immutable and ArcGIS owns the claim bound to it. Recreating
the volume while that claim exists leaves it Released with a stale claim
reference, which never rebinds and reports only as a binding timeout.
Deregister the data store first, confirm the claim is gone, then replace the
volume and re-register with exact `datasetRoot`. A `Released` volume with a
stale claim reference is also not something an upgrade cleans up: `helm upgrade`
does not reconcile it and it never rebinds, so delete the object explicitly and
confirm it is absent afterwards. With `reclaimPolicy: Retain`, deleting the
object touches no node filesystem content, so the mirror is not at risk from
that deletion.

**Verify what republishing does to placement.** If republishing a service resets
its node affinity and tolerations, every data update needs manual intervention
to re-place the pods. Test this once on your version before planning around it.

## SELinux labelling of the shared tree

**A volume is relabeled only if the plugin supports SELinux and the filesystem
carries `seclabel`.** Both tests must pass, which is why the behaviour is
asymmetric across volume types rather than uniform. The in-tree `hostPath`
plugin fails the first test and never relabels anything. NFSv4.1 mounted without
`security_label` fails the second and presents one fixed context for the whole
mount, so it cannot be relabeled per file either. A `local` volume on an
`xfs rw,seclabel` filesystem passes both, and mount setup then relabels the tree
recursively to the mounting pod's randomly assigned MCS categories. Check
`/proc/mounts` for `seclabel` on the backing filesystem, and
`CSIDriver.seLinuxMount` for whether `-o context=` is even available. One
relabeling consumer redefines the tree's label for every other consumer on that
node, and mode `0755` root:root does not help — Unix mode bits do not override an
MCS mismatch. The two checks are:

```bash
grep -E 'geodata|gisdata' /proc/mounts
kubectl get csidriver -o custom-columns='NAME:.metadata.name,SELINUXMOUNT:.spec.seLinuxMount'
```

The two mount lines observed on a pool node show the asymmetry directly:
`seclabel` on the instance store, absent on the NFS source.

```text
/dev/nvme2n1 /mnt/geodata xfs rw,seclabel,noatime,attr2,inode64,logbufs=8,logbsize=32k,noquota 0 0
svm-….fsx.us-west-2.amazonaws.com:/gisdata /mnt/source nfs4 ro,relatime,vers=4.1,rsize=65536,wsize=65536,hard,proto=tcp,nconnect=16,sec=sys,… 0 0
```

On the one cluster this was investigated on, `seLinuxMount` was `false` for
`nfs.csi.k8s.io`, `ebs.csi.aws.com`, `efs.csi.aws.com`, and `smb.csi.k8s.io`.
That is one cluster's observation, not a general guarantee; the second command
above answers the question for your own. A `ReadWriteOnce` EBS volume is the case
to watch, and it is explicitly unverified here: it probably passes both tests and
therefore probably is relabeled. It does not bite in this deployment because each
such volume has a single consumer, and it was never tested here, so check for
`seclabel` before sharing one between consumers.

The NFS source is the same rule seen from the other side. Six ArcGIS pods holding
six distinct MCS pairs — `c726,c972`, `c122,c402`, `c151,c210`, `c115,c464`,
`c550,c797`, `c896,c990` — two of them co-located on one node, all share
`ontap-gisdata-pvc` (`ReadOnlyMany`, NFS) and all are healthy. That is the same
sharing pattern that broke on the `local` PV, working on a filesystem without
`seclabel`.

**A category-free label is the shareable state.** Any `container_t` process can
access an object that carries no MCS categories, whatever categories the process
itself holds. That is why consumers with unrelated category pairs read the same
tree successfully for months. The goal is therefore to ensure nothing relabels
the tree, not to align categories across consumers.

**Distinct paths are necessary but not sufficient.** Labels live on inodes, not
on paths. A bind mount, a hard link, or a symlink that exposes one tree under two
paths exposes it to a relabel through either one. Chart validation is lexical —
`helm template` cannot resolve symlinks, bind mounts, or hard links — so two
distinct strings that alias the same inodes pass validation and still break at
runtime. Ensure distinct inodes, not just distinct paths.

**The relabel is on disk and outlives the consumer.** The `local` plugin writes
per-inode labels with `setxattr`; it does not use a `context=` mount option.
Deregistering the data store, deleting the PVC, and removing the pod therefore do
not restore access — the label stays exactly as the last mounting pod left it.
The discriminator is `seclabel` versus `context=` in `/proc/mounts`: `seclabel`
means labels are per inode and persist, `context=` means the label is a property
of the mount and disappears with it.

**The relabel changes the type as well as the level.** Healthy nodes carry
`system_u:object_r:local_t:s0`; the affected node carried
`system_u:object_r:data_t:s0:c20,c476`. Clearing the categories alone would still
leave the wrong type, so any repair that only targets the MCS level is
incomplete.

**You cannot relabel it back on Bottlerocket.** `chcon -R` from a
`--profile=sysadmin` debug container reads the entire tree and fails every
`setxattr` with `Permission denied`, and `restorecon` and `matchpathcon` are not
present in the `amazonlinux:2023` debug image. **Replace the node.** That works
because the mirror is derived from an authoritative source and is re-created by
the first sync on the replacement, so nothing unique is lost. Budget for one
first sync; see [../RESULTS.md](../RESULTS.md).

**New files do not inherit the writer's categories.** The sync agent runs as
`container_t:s0:c750,c987` and the files it writes come out
`system_u:object_r:local_t:s0` with no categories at all. A clean tree therefore
stays clean across syncs, and one label confirmation after a completed pass is
enough — the agent's own categories are not a slow leak into the tree.

**MCS categories are per pod sandbox, not per service.** Two pods of the one
ReplicaSet `arcgis-system-publishingtools-gpsyncserver-dgo59-b5cfc74f9`, both on
node `ip-10-0-37-12`, held `c372,c821` and `c388,c1019`. Nothing can be aligned
by service, Deployment, or ReplicaSet, because the pair is assigned per sandbox
and reassigned on every pod creation.

**Readiness labels are an inventory, not a health signal.** A node advertised the
correct dataset generation and count, carried no `data-not-ready` taint, and every
pod on it reported ready, while its tree was completely unreadable to those pods.
The labels describe what top-level names were last enumerated, not whether a
consumer can read them. Verify access as the consuming identity — read the tree
from a pod, not from a debug container — before treating a node as healthy.

## Karpenter

**Automated node image rotation must be disabled for nodes holding mirrored
data.** Drift replacement discards the local copy and re-runs the sync on a
schedule set by the OS release cadence.

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
improvement, and that result carries no information about connections.
Establish which resource is binding first.

**Change one thing.** Throughput, IOPS and cache state moving together produced
several confident and wrong conclusions on the way to the numbers in these
notes.

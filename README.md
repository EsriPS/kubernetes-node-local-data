# Node-local data stores for routing and geocoding

Routing and geocoding services read large file-based datasets. This project
provisions a dedicated Karpenter node pool on AWS, mirrors an authoritative NFS
export onto each node's instance-store NVMe, and exposes that mirror to ArcGIS
Enterprise on Kubernetes.

The Helm chart is the maintained deployment source. Sample code is provided
as-is and unsupported. The historical performance test copied roughly 500 GB to
two nodes in about five minutes; see [RESULTS.md](RESULTS.md) for the test
conditions and limitations.

## Table of contents

- [What this is for](#what-this-is-for)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Configure the chart](#configure-the-chart)
- [Install the chart](#install-the-chart)
- [Register the ArcGIS data store](#register-the-arcgis-data-store)
- [Place ArcGIS workloads](#place-arcgis-workloads)
- [Verify the deployment](#verify-the-deployment)
- [Why the mirror must not be relabeled](#why-the-mirror-must-not-be-relabeled)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Not covered](#not-covered)
- [Tuning notes](#tuning-notes)
- [Official documentation](#official-documentation)
- [Licensing](#licensing)

## What this is for

Use this project for large, read-only, file-based locators and network datasets
that change infrequently. A network dataset with quarterly releases is the case
it was built for. It is a poor fit for frequently changing data and unnecessary
when a network share already meets the workload's latency and throughput needs.

## How it works

The ONTAP or other NFS export is authoritative. A sync-agent DaemonSet mirrors
that source directly into the configured `datasetRoot` on every dedicated node.
The default and all commands in this guide use the exact path `/mnt/geodata`.
If you override `datasetRoot`, use that exact rendered value for the EC2 bind,
Path Shim hostPath, readiness checks, and the ArcGIS registration.

The NodePool starts nodes with a data-not-ready taint. After a successful first
sync, the agent records dataset-generation and dataset-count labels and removes
that taint. The permanent dedicated taint remains, so only workloads configured
for the `geodata` workload can schedule there.

```text
NFS export ──> sync-agent DaemonSet ──> /mnt/geodata
                                               │
                                               v
                              one hostPath folder data store
                                 (routing and geocoding)
```

**One directory, one mount type.** Routing and geocoding both read the mirror
through the single organization-wide hostPath folder data store. The chart
renders no `local` PersistentVolume, and it refuses to render if a `localVolume`
key is present, because the `local` volume plugin relabels the tree it mounts
and would lock every other consumer out of `/mnt/geodata`. See
[Why the mirror must not be relabeled](#why-the-mirror-must-not-be-relabeled).

The optional Path Shim creates `/mnt/geodata` on every eligible cluster node so
ArcGIS can mount that organization-wide hostPath. On dedicated nodes the path is
the instance-store mirror; on other nodes it is an empty directory.

### Mirror consistency and source publication

ONTAP is the authoritative source, and `datasetRoot` is its direct mirror. Each
pass runs exactly one recursive `rclone sync <source>/ <datasetRoot>/`.
Included files added or changed on ONTAP appear in the mirror, and destination
files removed from ONTAP are deleted, subject to the configured excludes.
Node-local content is not retained independently of its source.

Rclone's default comparison uses file size and modification time. Publishing a
same-named source file must therefore change its size or modification time for
the agent to detect it without checksum mode. The agent deliberately does not
use `--inplace`: rclone transfers changed content to a temporary file and
renames that file over the destination only after the transfer completes. A
final file path is therefore replaced atomically per file, but the whole tree is
not an atomic snapshot. Consumers can observe a mix of old and new files while
a pass is running; coordinate source publication with workload draining or
restarts when a release requires cross-file atomicity.

There is no second full mirror tree. Size instance storage for the complete
source mirror, rclone's temporary files for transfers and replacements, and
operational headroom. If a pass is interrupted, completed additions,
replacements, or deletions are not rolled back. The agent retries on the next
interval, and a later successful pass reconciles the mirror and any transfer
temporary files.

After a successful pass, the dataset-generation label hashes the sorted
**top-level names** in `datasetRoot`, and dataset-count records their count. The
generation label identifies that top-level dataset set; it is not a checksum of
file contents, so an in-place content update beneath the same names does not
change it.

## Prerequisites

- Kubernetes 1.25 or later on Amazon EKS.
- ArcGIS Enterprise on Kubernetes 12.0 or later.
- Helm CLI 3.0.0 or later.
- Karpenter with the `karpenter.sh/v1` NodePool API and
  `karpenter.k8s.aws/v1` EC2NodeClass API.
- Bottlerocket nodes with instance-store NVMe.
- The NFS CSI driver `nfs.csi.k8s.io`.
- An existing NFS export reachable from the selected subnets and security
  groups. FSx for NetApp ONTAP was used for the measured configuration.
- A container registry and Docker-compatible build environment for the agent
  image.

Verify the client and cluster prerequisites before installation:

```bash
helm version --short
kubectl api-resources --api-group=karpenter.sh -o wide | grep -E 'karpenter\.sh/v1[[:space:]].*NodePool'
kubectl api-resources --api-group=karpenter.k8s.aws -o wide | grep -E 'karpenter\.k8s\.aws/v1[[:space:]].*EC2NodeClass'
kubectl get csidriver nfs.csi.k8s.io
```

If the NFS CSI driver is not installed, follow its upstream installation
instructions. One Helm-based installation is:

```bash
helm repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
helm repo update
helm install csi-driver-nfs csi-driver-nfs/csi-driver-nfs --namespace kube-system
```

The sync agent can patch Kubernetes nodes to manage readiness taints and labels.
Its ClusterRole is limited to `get`, `list`, `watch`, and `patch` on nodes, but
that permission is cluster-wide and should be treated as a trust boundary.

## Configure the chart

Copy the documented environment example and keep the resulting file under your
normal configuration controls:

```bash
cp examples/values-example.yaml my-values.yaml
helm show values charts/node-local-data
```

Set the installation-specific values in `my-values.yaml`:

- `labelDomain`: a DNS domain you control for labels and taints.
- `image.repository`: the registry path that contains the agent image.
- `nfs.server`, `nfs.shareName`, and `nfs.sharePath`: the source export.
- `nfs.capacity`: the declared capacity used by both the source PV and PVC.
- `aws.amiAlias`: a tested, pinned Karpenter AMI alias in
  `family@version` format, for example `bottlerocket@v1.64.0`. This is a
  Karpenter selector—not an EC2 AMI ID such as `ami-...` or an EC2 AMI name
  such as `bottlerocket-aws-k8s-...`.
- `aws.nodeRole`: the IAM role used by Karpenter-launched nodes.
- `aws.subnetSelectorTerms` and `aws.securityGroupSelectorTerms`: native
  EC2NodeClass selector terms.
- `nodePool.zones`: one or more availability zones.

The chart renders `aws.amiAlias` as `spec.amiSelectorTerms[].alias` on the
EC2NodeClass. Karpenter resolves that alias to an EKS-optimized AMI for the
cluster's Kubernetes version and AWS Region. To list available Bottlerocket
versions from AWS Systems Manager Parameter Store, set your control-plane
version and run:

```bash
K8S_VERSION=1.34
aws ssm get-parameters-by-path \
  --path "/aws/service/bottlerocket/aws-k8s-$K8S_VERSION" \
  --recursive \
  --query 'Parameters[].Name' \
  --output text |
  tr '\t' '\n' |
  grep -v '/latest/' |
  awk -F/ '{print $7}' |
  sort -u
```

Prefix the selected semantic version with `v` in the Karpenter alias; for
example, SSM version `1.64.0` becomes `bottlerocket@v1.64.0`. See Karpenter's
[EC2NodeClass AMI selector documentation](https://karpenter.sh/docs/concepts/nodeclasses/#specamiselectorterms).

The documented example uses tag-based subnet and security-group selectors, one
zone, and `nfs.capacity: 1536Gi`. `workloadValue` defaults to `geodata` in
`charts/node-local-data/values.yaml` and does not need to be repeated in
`my-values.yaml`. If you override it, update the commands and ArcGIS placement
values below to match the rendered chart.

Build and push the supported agent image to Amazon ECR. Replace the sample
account ID, Region, and namespace before running these commands. The ECR
repository is `<namespace>/geodata-sync-agent`, while `image.repository` is the
registry and optional namespace prefix because the chart appends
`geodata-sync-agent:0.5.0`.

```bash
# Replace these three sample values for your AWS environment.
AWS_ACCOUNT_ID="123456789012"
AWS_REGION="us-west-2"
ECR_NAMESPACE="team"

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPOSITORY="${ECR_NAMESPACE}/geodata-sync-agent"
IMAGE_URI="${ECR_REGISTRY}/${ECR_REPOSITORY}:0.5.0"

# Authenticate Docker and create the ECR repository once if it does not exist.
aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "$ECR_REGISTRY"
aws ecr describe-repositories \
  --region "$AWS_REGION" \
  --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1 ||
  aws ecr create-repository \
    --region "$AWS_REGION" \
    --repository-name "$ECR_REPOSITORY" >/dev/null

cd agent
docker build --platform linux/amd64 -t geodata-sync-agent:0.5.0 .
docker tag geodata-sync-agent:0.5.0 "$IMAGE_URI"
docker push "$IMAGE_URI"
cd ..
```

The `docker tag` command is required: `docker push` only accepts a destination
tag that already exists in the local image store. A `tag does not exist` error
for the ECR URI means that exact local destination tag was not created (or the
source image was built under a different name or tag).

For the variables above, configure the chart with the prefix—not the final image
name or tag:

```yaml
image:
  repository: 123456789012.dkr.ecr.us-west-2.amazonaws.com/team
```

Replace the sample account ID, Region, and namespace in that value as well.

### Important defaults

The complete defaults are in `charts/node-local-data/values.yaml`. Operationally
important defaults include:

| Value | Default | Meaning |
|---|---:|---|
| `datasetRoot` | `/mnt/geodata` | Exact EC2 bind target, mirror root, Path Shim path, and the one ArcGIS hostPath registration path shared by routing and geocoding |
| `ec2NodeClass.dataVolumeSize` | `200Gi` | Bottlerocket data volume; it does not hold the dataset mirror |
| `nodePool.limits.nodes` | `3` | Explicit maximum node count |
| `nodePool.limits.cpu` | `144` | Explicit CPU limit, independent of node count |
| `warmCapacity.enabled` / `replicas` | `true` / `2` | Keep two distinct dedicated nodes provisioned |
| `pathShim.enabled` | `true` | Create `/mnt/geodata` on eligible nodes for the organization-wide hostPath registration |

There is no `localVolume` value. It was removed in chart 0.2.0 along with the
`geodata-local-sc` StorageClass and the `geodata-local-pv` PersistentVolume, and
a values file that still carries the key fails rendering with an explanation
rather than being silently ignored.

The source capacity, Bottlerocket data-volume size, and local NVMe instance
requirement are independent settings. The chart does not infer or convert one
from another. The default instance requirements select at least 2400 GB of local
NVMe by rendering Karpenter's `Gt: "2399"`; Karpenter uses GB for that
requirement, while Kubernetes capacities use quantities such as Gi.

The selected instance type must have enough instance-store space for the entire
source mirror, transfer-temporary files, and operational headroom. Nothing in
this chart imposes a quota on `/mnt/geodata`.

### Replace the instance requirement list

`nodePool.instanceRequirements` is one replaceable native Karpenter list. The
default list expresses measured category, generation, CPU, memory, local-NVMe,
and network floors. Karpenter validates scheduling feasibility; the chart does
not verify that replacement requirements provide enough storage, CPU, memory,
or network bandwidth.

To pin tested instance types, replace the entire default list rather than adding
a chart-specific mode:

```yaml
nodePool:
  instanceRequirements:
    - key: karpenter.k8s.aws/instance-type
      operator: In
      values:
        - r8id.8xlarge
        - i7i.8xlarge
```

`examples/values-instance-types.yaml` is a complete documented example with the
required environment values and an explicit instance-type requirement.

## Install the chart

One install brings up the whole chart. There is no second stage: with the local
PersistentVolume removed, nothing is held back to be enabled after the first
sync, and the only thing gated on first sync is ArcGIS publication. Helm owns
every rendered object; there are no keep annotations or install/uninstall hooks.

Set reusable shell variables. The release is stored in the `default` namespace,
while the chart creates its default workload namespace `geodata-sync`:

```bash
RELEASE=node-local-data
RELEASE_NAMESPACE=default
CHART=charts/node-local-data
VALUES=my-values.yaml
```

Before rendering or installing, check whether the workload namespace already
exists:

```bash
kubectl get namespace geodata-sync
```

On a new cluster, leave the default `namespace.create: true`; Helm creates and
owns `geodata-sync`. If the namespace already exists and is not owned by this
Helm release, do **not** add Helm ownership labels or annotations merely to make
the install pass. Configure it as externally managed in `my-values.yaml`:

```yaml
namespace:
  create: false
  name: geodata-sync
```

All namespaced chart resources still render into `namespace.name`, but Helm does
not try to create, adopt, or delete that Namespace. This avoids the `invalid
ownership metadata` installation error and prevents a later uninstall from
deleting a pre-existing namespace and unrelated resources in it. If
`namespace.name` does not exist while `namespace.create` is `false`, create it
explicitly before installation.

Review the render and install it:

```bash
helm lint --strict "$CHART" -f "$VALUES"
helm template "$RELEASE" "$CHART" --namespace "$RELEASE_NAMESPACE" -f "$VALUES" > rendered-install.yaml
helm install "$RELEASE" "$CHART" --namespace "$RELEASE_NAMESPACE" -f "$VALUES"
helm get notes "$RELEASE" --namespace "$RELEASE_NAMESPACE"
```

Watch the agent and inspect readiness labels. The label-domain example below
uses `data.example.com`; replace it with the configured domain, escaping dots
in the `custom-columns` JSONPath but not in the label selector:

```bash
kubectl logs -n geodata-sync daemonset/geodata-sync-agent -f
kubectl get nodes -l data.example.com/workload=geodata \
  -o custom-columns='NAME:.metadata.name,GEN:.metadata.labels.data\.example\.com/dataset-generation,COUNT:.metadata.labels.data\.example\.com/dataset-count,TAINTS:.spec.taints[*].key'
kubectl exec -n geodata-sync daemonset/geodata-sync-agent -- \
  python -c 'import os,sys; sys.exit(0 if any(os.scandir("/mnt/geodata")) else 1)'
```

Do not publish an ArcGIS service against the mirror until every intended
dedicated node has completed its first sync, reports dataset labels, and no
longer has the data-not-ready taint. A hostPath folder data store resolves to an
empty directory on a node that has no data yet, so publishing early fails at
query time rather than at mount time.

## Register the ArcGIS data store

Use `helm get notes` after each install or upgrade to obtain the release-specific
names, placement values, and exact `datasetRoot`.

### One shared hostPath folder data store

When `pathShim.enabled` is true and its DaemonSet is Ready, register the exact
hostPath `/mnt/geodata` through the ArcGIS Admin REST API, and use that **one**
registration for both routing and geocoding. There is no separate geocoding
registration and no PersistentVolume to register.

A hostPath folder data store cannot be registered from ArcGIS Enterprise
Manager, which offers PV-based registration only. Use the Admin REST API and
pass `allowServicesRestart`, because the call restarts service pods. Wait for the
Path Shim DaemonSet to be Ready before registering.

HostPath registration is organization-wide: ArcGIS may add the mount to service
Deployments on any node, which is why the Path Shim creates the same path outside
the dedicated pool. Read [tuning/gotchas.md](tuning/gotchas.md) before
registration.

A hostPath folder data store is never SELinux-relabeled, so both workloads read
the same tree without changing what the other sees on disk. That is the whole
reason geocoding is served this way; see
[Why the mirror must not be relabeled](#why-the-mirror-must-not-be-relabeled).

### What the single-hostPath model gives up

Three things, all accepted deliberately:

**Registration moves from Enterprise Manager to the Admin REST API.** A
PersistentVolume-based data store can be registered in Enterprise Manager with a
StorageClass, access mode, and capacity. A hostPath store is registered through
the Admin REST API and its mount scope is organization-wide rather than scoped to
one claim. Routing has been registered this way from the start, so geocoding
joining it is not a new exposure — it is the exposure that already existed, now
covering one more service.

**The failure mode when data is missing gets quieter.** A `local` volume fails
loudly: a pod scheduled onto a node without the data never starts, and
`MountVolume.SetUp` says so. A hostPath store on the same node mounts an empty
directory and fails later, at query time, with a less obvious error. This is the
trade the Path Shim already makes for routing. It is why the first-sync gate
above and the node affinity in
[Place ArcGIS workloads](#place-arcgis-workloads) still matter: they are what
keeps a consumer off a node that holds no mirror.

**Whole-dataset publication atomicity is gone.** The earlier `manifests/`
deployment staged each dataset and published it with a rename, so a service pod
saw either a complete dataset directory or no directory at all. One shared tree
mirrored in place cannot offer that: rclone still replaces each *file* by rename,
never with `--inplace`, so no consumer reads a half-written file, but additions,
replacements, and deletions become visible at different moments within a pass. A
release that needs cross-file consistency has to drain or restart its consumers as
part of source publication. See
[Mirror consistency and source publication](#mirror-consistency-and-source-publication).

## Place ArcGIS workloads

Use the chart's permanent dedicated-node placement for services that must read
the mirrored data. For a `labelDomain` of `data.example.com`, the placement
values are:

```yaml
tolerations:
  - key: data.example.com/dedicated
    operator: Equal
    value: geodata
    effect: NoSchedule
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          - key: data.example.com/workload
            operator: In
            values:
              - geodata
```

If warm capacity is disabled, the first ArcGIS workload waits for Karpenter to
provision a node and for that node's first mirror sync to finish.

PublishingTools validates registered data while publishing. Configure its pod
placement policy in ArcGIS Enterprise Manager so it uses the dedicated
`geodata` nodes and the toleration and affinity shown above.

ArcGIS Enterprise on Kubernetes does not currently accept placement properties
for every service during publication. Configure the default deployment
properties for MapServer, GPServer, and GPServerSync services using the
ArcObjects11 provider, or set placement immediately after publishing where the
workflow permits. Routing publication can fail before that later adjustment, so
configure its default placement before publishing.

See the ArcGIS Enterprise Admin REST API documentation for
[Default Deployment Properties](https://developers.arcgis.com/rest/enterprise-administration/enterprise/deployment-default-properties/).

## Verify the deployment

Check from the sync-agent pod, which only schedules on the dedicated pool. The
exact mirror path is `/mnt/geodata` inside both the container and host mount:

```bash
kubectl exec -n geodata-sync daemonset/geodata-sync-agent -- df -hT /mnt/geodata
```

Expect an instance-store device, often an `md` array when the instance type has
multiple NVMe disks. A filesystem matching the Bottlerocket data-volume size
instead indicates that the EC2 bind did not place `/mnt/geodata` on instance
storage.

Check the Path Shim on a node outside the dedicated pool. The exact path should
exist and normally be empty there:

```bash
NODE=$(kubectl get nodes -l 'data.example.com/workload!=geodata' -o jsonpath='{.items[0].metadata.name}')
POD=$(kubectl get pods -n geodata-sync -l app=geodata-path-shim --field-selector spec.nodeName="$NODE" -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n geodata-sync "$POD" -c keep-path -- df -hT /mnt/geodata
kubectl exec -n geodata-sync "$POD" -c keep-path -- sh -c 'test -z "$(find /mnt/geodata -mindepth 1 -maxdepth 1 -print -quit)"'
```

Replace `data.example.com` with the configured label domain. Confirm every
dedicated node has the expected dataset labels and only the permanent dedicated
taint before publishing dependent services.

## Why the mirror must not be relabeled

**One directory, one mount type, and the tree stays category-free.** Bottlerocket
runs SELinux enforcing and gives each pod sandbox a random MCS category pair. An
object that carries no categories is readable by any `container_t` process
whatever its own categories, which is what lets every ArcGIS consumer share
`/mnt/geodata`. A hostPath folder data store never relabels the tree; a `local`
PersistentVolume over the same tree does, recursively and on disk, to one pod's
categories. A healthy node reads `system_u:object_r:local_t:s0`; the node that
was relabeled here read `system_u:object_r:data_t:s0:c20,c476` and became
unreadable to routing, the Path Shim, and the sync agent alike. The single rule
is therefore: nothing may mount `/mnt/geodata` in a way that relabels it. The
chart enforces that by rendering no `local` volume, rejecting a leftover
`localVolume` key, and guarding any future relabeling volume against the shared
roots.

**Aligning categories instead is not available.** ArcGIS pod specifications
cannot be modified — their only `securityContext` is
`{"supplementalGroups":[117932853]}` — and categories are assigned per pod
sandbox, not per service: two pods of the one ReplicaSet
`arcgis-system-publishingtools-gpsyncserver-dgo59-b5cfc74f9` on node
`ip-10-0-37-12` held `c372,c821` and `c388,c1019`. There is nothing to pin.

**`chmod 777`, permissive mode, and privileged consumers do not help**, because
Unix mode bits do not override an MCS mismatch. The denied directory was
`drwxr-xr-x` root:root before, during, and after the incident, and the same 0755
root:root mode over a category-free label read fine on every other node. The
mode was never the variable, and relaxing enforcement cluster-wide trades a
security control for a problem that has a configuration answer.

The mechanism behind all of this — the two conditions that make a volume
relabelable, how to check them, why the NFS source is exempt, and why distinct
paths are not the same as distinct inodes — is in
[tuning/gotchas.md](tuning/gotchas.md) under "SELinux labelling of the shared
tree". Read it before sharing any tree with a new consumer.

## Operations

Six runbooks. Upgrades and node replacement are routine; the migration and
recovery procedures are ordered sequences where the order is the point.

### Upgrade safely

Use the same values file for every upgrade. Review the complete render before
applying it:

```bash
helm lint --strict "$CHART" -f "$VALUES"
helm upgrade "$RELEASE" "$CHART" --namespace "$RELEASE_NAMESPACE" -f "$VALUES" --dry-run > rendered-upgrade.yaml
helm upgrade "$RELEASE" "$CHART" --namespace "$RELEASE_NAMESPACE" -f "$VALUES"
helm get notes "$RELEASE" --namespace "$RELEASE_NAMESPACE"
```

Helm applies its normal three-way merge and deletes release resources disabled
by the new values. Changes to the NodePool or EC2NodeClass, including the pinned
AMI and instance requirements, can mark NodeClaims `Drifted`. The NodePool keeps
`expireAfter: Never`, `consolidateAfter: Never`, `consolidationPolicy:
WhenEmpty`, and a zero voluntary-disruption budget, so review drift and replace
NodeClaims deliberately rather than expecting an automatic rollout.

#### Before disabling the Path Shim

Before changing `pathShim.enabled` from `true` to `false`, deregister the shared
hostPath data store `/mnt/geodata` in ArcGIS and stop workloads that use it.
Only then review and run `helm upgrade`. Disabling the DaemonSet does not delete
node filesystem content, but it removes the component that guarantees the path
exists on every eligible node.

### Upgrading from a release that had the local volume

Chart 0.2.0 removes the `geodata-local-sc` StorageClass, the `geodata-local-pv`
PersistentVolume, and the `localVolume` value. Upgrading from chart 0.1.0 deletes
those two objects and leaves every other rendered resource alone. Coming from the
raw `manifests/` tree instead, read
[Migrating from the raw manifests](#migrating-from-the-raw-manifests) first: that
path also moves the registered data-store path and rebuilds each node's mirror.
Before the upgrade:

1. Deregister the geocoding local-PV data store in ArcGIS Enterprise, and
   register geocoding against the shared hostPath store instead.
2. Delete the ArcGIS-owned claim bound to `geodata-local-pv` and verify it is
   gone. **Deleting a PV that still has a live claim leaves the claim unbound
   and surfaces only as a binding timeout**, which is why this order matters.
3. Remove the `localVolume` block from your values file. It is not merely
   ignored: rendering fails with an explanation if the key is still present.
   Pass `-f "$VALUES"` and not `--reuse-values`, or the old keys are fed back in
   and trip that rejection.
4. Run the upgrade, then confirm the PV is actually absent. A `Released` PV with
   a stale `claimRef` never rebinds and `helm upgrade` does not reconcile it, so
   delete it explicitly if it survived. `reclaimPolicy: Retain` means deleting
   the object touches no node filesystem content and the mirror is untouched.

```bash
kubectl get pv geodata-local-pv -o jsonpath='{.status.phase}{"\n"}' 2>/dev/null || echo absent
kubectl delete pv geodata-local-pv     # only if it is still present after the upgrade
```

Do not roll this upgrade back automatically. Helm's rollback recreates
`geodata-local-pv` and `geodata-local-sc`, which reinstates the only object in
the chart able to relabel the shared tree.

### Migrating from the raw manifests

Releases before the chart shipped the numbered `manifests/` tree, whose sync
agent staged each dataset into `/mnt/geodata/staging/<dataset>` and published it
with a rename into `/mnt/geodata/active/<dataset>`. Both the Path Shim and the
local PV registered **`/mnt/geodata/active`**. The chart mirrors the source
directly into `datasetRoot` and registers **`/mnt/geodata`**.

Two consequences, neither of which the chart can handle for you:

**The registered path changes.** A data store registered at
`/mnt/geodata/active` keeps resolving after the upgrade — the Path Shim creates
`datasetRoot` with `DirectoryOrCreate`, so `active/` is simply an empty
subdirectory once the new agent has run. An empty directory mounts successfully
and fails at query time, which is the quiet failure described in
[What the single-hostPath model gives up](#what-the-single-hostpath-model-gives-up).
Re-register at `/mnt/geodata` as part of this migration.

**Every node re-transfers its mirror once.** The new agent runs
`rclone sync <source>/ <datasetRoot>/`, and `sync` deletes destination paths the
source does not have. `active/` and `staging/` are not in the source, so the
first pass removes the old layout and re-transfers the whole tree. Budget the
first-sync figure from [RESULTS.md](RESULTS.md): 58.9 minutes for about 840 GiB
on this cluster, per node, with the source serving those reads.

Sequence, one node pool at a time:

1. Deregister the geocoding local-PV data store and the routing
   `/mnt/geodata/active` folder data store in ArcGIS Enterprise. Delete the
   ArcGIS-owned claim bound to `geodata-local-pv` and confirm it is gone.
2. Delete the old DaemonSets so the previous agent stops writing while the
   layout changes:
   `kubectl delete daemonset geodata-sync-agent geodata-path-shim -n geodata-sync`.
3. Install the chart. `helm install` rather than `helm upgrade` if the old
   deployment was applied with `kubectl apply` and Helm never owned it.
4. Wait for the first pass on every intended node, then confirm the mirror is at
   `/mnt/geodata` and that `active/` and `staging/` are gone.
5. Register one hostPath folder data store at `/mnt/geodata` for both routing and
   geocoding, per
   [Register the ArcGIS data store](#register-the-arcgis-data-store).
6. Confirm the SELinux label on the tree once, as
   [Recover a node whose mirror is unreadable](#recover-a-node-whose-mirror-is-unreadable)
   describes. A node that consumed the old local PV before this migration may
   already carry a relabeled tree, and that condition survives the migration —
   the label is on the inodes, and the instance store is not rebuilt by
   reinstalling the chart. Such a node needs replacing.

```bash
kubectl exec -n geodata-sync daemonset/geodata-sync-agent -- ls -1 /mnt/geodata
# expect dataset names only: no active/, no staging/
```

### Replace the source PersistentVolume

`geodata-source-pv` is a static PV — the only one the chart renders. Kubernetes
can reject in-place changes to immutable fields, and advertised capacity changes
are not a substitute for resizing the underlying NFS export. A replacement may be
required when changing capacity, NFS identity, StorageClass, or other immutable
fields.

Plan a maintenance window and use this sequence:

1. Deregister the ArcGIS data store if the change also moves `datasetRoot`.
2. Ensure the old PV is unbound: stop the sync-agent DaemonSet, delete
   `geodata-source-pvc`, and wait for the claim to disappear.
3. Delete the old unbound `geodata-source-pv` and verify that it no longer
   exists.
4. Update the independent value (`nfs.capacity`, the NFS settings, or
   `datasetRoot`) and review the rendered upgrade.
5. Run `helm upgrade`; Helm recreates any manually removed release resources.
6. Wait for `/mnt/geodata` to be mirrored and ready on every intended node, then
   re-register the ArcGIS data store if its path changed.

For the default source resources, the maintenance commands include:

```bash
kubectl delete daemonset geodata-sync-agent --namespace geodata-sync
kubectl delete persistentvolumeclaim geodata-source-pvc --namespace geodata-sync
kubectl wait --for=delete persistentvolumeclaim/geodata-source-pvc --namespace geodata-sync --timeout=5m
kubectl delete persistentvolume geodata-source-pv
kubectl wait --for=delete persistentvolume/geodata-source-pv --timeout=5m
helm upgrade "$RELEASE" "$CHART" --namespace "$RELEASE_NAMESPACE" -f "$VALUES"
```

Adjust names when `namePrefix` or `namespace.name` differs. Deleting the source
PV object does not delete the authoritative NFS export. Do not delete or resize
the source export as part of this chart procedure.

### Replace a node

Nodes in this pool are not voluntarily replaced by the chart's disruption
settings. Inspect drift before replacement:

```bash
kubectl get nodeclaims -l karpenter.sh/nodepool=geodata-nodes \
  -o custom-columns=NAME:.metadata.name,DRIFTED:'.status.conditions[?(@.type=="Drifted")].status'
```

Replace one node at a time. Cordon it, relocate its workload, then delete its
NodeClaim. If the whole pool is cordoned together, Karpenter may provision
additional nodes up to the explicit limits. A replacement node does not accept
`geodata` service pods until its first mirror sync into `/mnt/geodata` succeeds.

Node replacement is also the recovery procedure when a node's mirror becomes
unreadable, which is the next runbook.

### Recover a node whose mirror is unreadable

**The signal to look for is the taint.** When the agent finds `datasetRoot`
present but unreadable it applies
`<labelDomain>/data-unreadable=replace-node:NoSchedule` to the node and leaves the
dataset-generation and count labels alone, because the mirror is still complete on
disk and rewriting those labels would destroy correct state. The taint is
therefore the machine-readable form of this condition:

```bash
kubectl get nodes -o custom-columns='NODE:.metadata.name,TAINTS:.spec.taints[*].key' \
  | grep data-unreadable
```

Earlier releases had the agent crash instead: `list_datasets` let
`PermissionError` escape and the pod entered `CrashLoopBackOff`, which is how this
was originally found — the affected node's agent went from **269 to 379 restarts**.
That crash is gone deliberately, because exiting the process also stopped the
agent from reconciling anything. Do not wait for a crash loop; the taint and the
per-interval `Dataset root is unreadable` warning in the agent log are the
signals now.

Other symptoms: routing map services on one node return readiness 503 and their
Deployments pass the progress deadline; the Path Shim cannot `ls` or `mkdir` its
path; the agent logs `lstat /mnt/geodata/<dataset>: permission denied` or
`PermissionError: [Errno 13]` — all against a directory that is still
`drwxr-xr-x` root:root. Confirm by reading the label from the Path Shim pod on
that node:

```bash
NODE=ip-10-0-37-101   # the affected node
POD=$(kubectl get pods -n geodata-sync -l app=geodata-path-shim --field-selector spec.nodeName="$NODE" -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n geodata-sync "$POD" -c keep-path -- ls -ldZ /mnt/geodata
```

A healthy node reads `system_u:object_r:local_t:s0`. The affected node here read
`system_u:object_r:data_t:s0:c20,c476` — the MCS categories of one pod sandbox,
and a different SELinux type as well.

**Replacing the node is the procedure.** It needs no relabel permission, no
privileged container, and no host setting change, and it works because the mirror
is derived from an authoritative source: discarding a node's copy costs a resync,
not data.

**Prerequisite, and the order matters.** Remove whatever relabeled the tree
before you replace anything, or the replacement is poisoned in turn. Chart 0.2.0
onwards cannot create such a mount, so on a current release there is nothing to
remove and you can go straight to the steps below. Coming from an earlier release,
work through
[Upgrading from a release that had the local volume](#upgrading-from-a-release-that-had-the-local-volume)
first: deregister the data store, delete the ArcGIS-owned claim, then remove the
PersistentVolume, in that order.

**Removing the mount does not repair the node.** The label is written per inode on
disk, not carried by the mount, so deregistering the data store, deleting the
claim, and removing every referencing pod leaves the denial exactly where it was.
That is what the 269-to-379 restart count above measured: it accumulated *across*
the removal of the data store, the claim, and the PV, with the denial unchanged
at the end of it. The node does not self-heal, which is why replacement is not
optional.

With the source removed, replace the node:

1. Cordon the affected node.
2. **Relocate its ArcGIS workloads.** Cordon alone evicts nothing; without this
   step the broken node keeps serving requests it cannot satisfy.
3. Let the evicted warm-capacity placeholder force Karpenter to provision a
   replacement. That eviction is the mechanism, so warm capacity has to be
   enabled.
4. Wait for the new node to complete its first sync and clear the
   data-not-ready taint.
5. Confirm the replacement: label, mirror size, published dataset generation and
   count, data-not-ready taint cleared, and no `data-unreadable` taint. The agent
   clears that one itself on the first readable pass, so its presence on a node
   that has finished syncing means the replacement is affected too.
6. Delete the old NodeClaim. The bad labels go with the instance store.

**Blast radius: one node.** Its ArcGIS workloads move, its mirror is discarded
and re-derived from the authoritative source, and no other node is touched.
Nothing in this procedure deletes, moves, or re-syncs any other node's mirror.
Discarding one node's copy is the node pool's own premise, and the documented
behavior of any replacement here.

**Budget for one full first sync.** On this cluster the replacement transferred
`901830389268` bytes in `3533.9s` — **58.9 minutes for about 840 GiB, roughly
255 MB/s** — while the source was concurrently serving live reads from the
remaining node. Plan for that read as well as the elapsed time.

**Confirm the label once after the first sync, not on a schedule.** New files do
not inherit the writing process's MCS categories: the agent runs as
`container_t:s0:c750,c987` and every file it writes is `local_t:s0` with no
categories. A clean tree stays clean, so one post-sync confirmation is enough.

#### Relabeling in place: denied on Bottlerocket 1.64.0

Recorded so nobody rediscovers the denial from scratch. **This is not a runbook
step.** `chcon -R` run from `kubectl debug node/<node> --profile=sysadmin` with an
`amazonlinux:2023` image walked the entire tree — it could read every filename,
which by itself shows a privileged context bypasses MCS for reads — and **every
`setxattr` returned `Permission denied`.** Reading a labeled object and relabeling
it are governed by different permissions (`relabelfrom` and `relabelto`), and
Bottlerocket's policy does not grant the second to that context.

Cost is not the obstacle: `df -i` reported `IUsed 174` for 840 GB, so a full
relabel is about 174 `setxattr` calls. Permission is the obstacle.

Tooling notes for anyone who tries anyway, because several of these cost time:

- `chcon` is present, from coreutils.
- `matchpathcon` and `restorecon` are **absent** — policycoreutils is not shipped
  in that image, so "let policy decide the target label" is not available either.
- `mount` is absent from the minimal image.
- `id -Z` fails, because `/sys/fs/selinux` is not in the debug container's mount
  namespace and that is what libselinux probes.

If a future Bottlerocket release does grant `relabelto`, note that clearing
categories is not sufficient: the target must be the full reference label
captured from a healthy node, because the relabel moved the type too. Verify by
histogram match against that node, not by absence of categories — a `data_t:s0`
tree has no categories and is still wrong.

### Uninstall safely

Before uninstalling:

1. Deregister the shared hostPath data store `/mnt/geodata` in ArcGIS if it was
   registered.
2. Stop dependent ArcGIS workloads.
3. Only then uninstall the release.

No ArcGIS-owned PersistentVolumeClaim is involved. The chart no longer renders a
PersistentVolume that ArcGIS can claim; the only claim it owns is
`geodata-source-pvc` for the NFS source, held by the sync agent.

```bash
helm uninstall "$RELEASE" --namespace "$RELEASE_NAMESPACE"
```

Uninstall requests deletion of every object rendered by the release, including
NodePool, EC2NodeClass, the source StorageClass and PV, Path Shim, and
warm-capacity resources. The source StorageClass and PV use a `Retain` reclaim
policy, but reclaim policy governs storage reclamation after claim release; it
does not keep the Kubernetes objects installed during `helm uninstall`.

Kubernetes PV protection can leave a bound PV in `Terminating` until its
external claim is removed. The chart has no hook or force-deletion mechanism to
bypass that protection and never deletes ArcGIS claims on the operator's behalf.
The chart also has no uninstall command that deletes files from the NFS export
or node filesystems.

Uninstalling the NodePool can terminate its nodes and permanently discard their
instance-store copies under `/mnt/geodata`. The NFS export remains the
authoritative recoverable source; a later installation must rebuild every local
mirror.

## Troubleshooting

### A PV-backed data store appears at a different path inside the pod

ArcGIS remaps a PersistentVolume-based folder data store. It appears in-pod at
`/arcgis/volumes/pvc/<derived>/<registered-path>`, where `<derived>` comes from
the CSI volume handle, and is the literal string **`noid`** when there is no CSI
handle. So a `local` PV registered with volume path `/mnt/geodata` shows up as:

```text
/arcgis/volumes/pvc/noid/mnt/geodata
```

A hostPath folder data store is **not** remapped: it appears at the host path
itself, `/mnt/geodata`. Looking for the host path inside a PV-backed pod, finding
nothing, and concluding the mount is missing is a live trap — it cost real time
during this investigation. Check `/arcgis/volumes/pvc/` before concluding
anything about a PV-backed store.

### Judging first-sync progress

`du -sh` overstates progress, because rclone preallocates space for files it has
not finished transferring. On the same tree mid-sync: `703G` of disk usage
against `569G` apparent size. Use `--apparent-size` for the honest number, and
look for `*.partial` files to see what is in flight:

```bash
kubectl exec -n geodata-sync daemonset/geodata-sync-agent -- \
  du -sh --apparent-size /mnt/geodata
kubectl exec -n geodata-sync daemonset/geodata-sync-agent -- \
  find /mnt/geodata -name '*.partial'
```

### Node readiness labels are an inventory, not a health signal

The dataset-generation and dataset-count labels hash the top-level names in
`datasetRoot`. They are not an access check. The affected node here advertised
the correct generation and count with no data-not-ready taint while its tree was
completely unreadable, and every ArcGIS pod on it reported ready. When
diagnosing, verify access as the consuming identity — read the tree from a pod —
rather than trusting the labels.

The `data-unreadable` taint closes the specific case the agent can observe: if its
own enumeration of `datasetRoot` is denied, the node is marked and the labels are
left alone. It is not a general health signal either. The agent runs as UID 0 with
its own MCS categories, so a tree that is readable to the agent and denied to a
particular service pod, or a dataset that is readable at the top level and denied
deeper in, produces no taint. Reading the tree as the consuming identity remains
the only check that answers the question directly.

## Not covered

**Automated placement after publishing.** Service pods need the pool affinity
and toleration configured through ArcGIS Enterprise Manager or the Admin REST
API. This chart does not modify ArcGIS resources.

**A tested multi-zone pool.** `nodePool.zones` accepts multiple zones, but the
published performance results cover one zone. Cross-zone NFS access can add cost
and latency; validate the complete design before expanding it.

**Instance suitability for custom requirements.** Karpenter determines whether
a requirement can schedule, but the operator must verify that selected instance
types provide enough NVMe, CPU, memory, and network bandwidth for the source
mirror and workload.

## Tuning notes

- [RESULTS.md](RESULTS.md) — what was measured, where, and what to re-measure
- [tuning/ontap.md](tuning/ontap.md) — source filesystem sizing and IOPS
- [tuning/nvme.md](tuning/nvme.md) — Bottlerocket instance-store binding
- [tuning/rclone.md](tuning/rclone.md) — sync engine behavior and tuning
- [tuning/gotchas.md](tuning/gotchas.md) — operational traps

## Official documentation

- [ArcGIS Enterprise on Kubernetes folder data stores](https://enterprise-k8s.arcgis.com/en/latest/administer/system-managed-data-stores.htm)
- [Karpenter](https://karpenter.sh)
- [Bottlerocket ephemeral storage](https://bottlerocket.dev/en/os/latest/api/settings/bootstrap-containers/)

## Licensing

Copyright 2026 Esri

Licensed under the Apache License, Version 2.0. A copy is in
[license.txt](license.txt).

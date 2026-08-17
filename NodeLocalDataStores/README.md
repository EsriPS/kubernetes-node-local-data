# Node-local data stores for routing and geocoding

Routing and geocoding services read large file-based datasets. Served over a
network file share, that read path is slower than reading from the node's own
disk. This guide stages a copy of the dataset onto each node in a dedicated
pool, gates the node until the copy is complete, and registers it with ArcGIS
Enterprise as a folder data store.

The sample stages roughly 500 GB onto two nodes in about five minutes. Sample
code and manifests, provided as-is and unsupported.

## Table of contents

- [What this is for](#what-this-is-for)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Apply order](#apply-order)
- [Registering the data stores](#registering-the-data-stores)
- [Verifying](#verifying)
- [Replacing a node](#replacing-a-node)
- [Not covered](#not-covered)
- [Tuning notes](#tuning-notes)
- [Official documentation](#official-documentation)

## What this is for

Use this when a dataset is large, changes rarely, and is read constantly. A
quarterly street network for routing and geocoding is the case it was built
for.

It is a poor fit when the data changes frequently, because every change means
staging a fresh copy onto every node in the pool. It is unnecessary when the
dataset is small enough that a network share keeps up.

## How it works

A DaemonSet runs on a dedicated node pool. On each pass it copies any dataset
the node does not yet hold from an NFS export onto the node's local NVMe.

Three properties make it safe to point a production service at:

**Data appears atomically.** Each dataset is copied into a staging directory and
moved into place with a rename. A service never sees a partially transferred
dataset, because the rename is atomic within a filesystem and the staging
directory sits on the same one.

**Nodes are gated until they hold data.** The pool applies a startup taint at
registration. The agent removes it after the first successful pass, which is
what releases service pods to schedule there. The agent also re-applies it if
the node's data disappears, which matters because instance-store data is lost
on a stop rather than only on termination.

**Nodes advertise what they hold.** After every successful pass the agent writes
a generation label to the node. Check it across the pool before publishing a
service that depends on a particular release.

```
NFS export ──> sync agent DaemonSet ──> /mnt/geodata/staging/<dataset>
                                              │ atomic rename
                                              v
                                        /mnt/geodata/active/<dataset>
                                              │
                            ┌─────────────────┴─────────────────┐
                            v                                   v
                  local PersistentVolume              hostPath folder data store
                       (geocoding)                           (routing)
```

## Requirements

- ArcGIS Enterprise on Kubernetes 12.0 or later
- Karpenter, with permission to create a NodePool and an EC2NodeClass
- Bottlerocket nodes with instance-store NVMe. The bootstrap commands in
  `manifests/02-ec2nodeclass.yaml` are Bottlerocket-specific
- `csi-driver-nfs` installed in the cluster. The in-tree NFS plugin needs a
  mount helper on the host that Bottlerocket does not ship:

  ```bash
  helm repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
  helm install csi-driver-nfs csi-driver-nfs/csi-driver-nfs -n kube-system
  ```

- An existing NFS export holding the dataset, reachable from the node pool's
  security group

## Configuration

Every environment-specific value is a `__PLACEHOLDER__` token. Replace all of
them before applying anything.

| Placeholder | Meaning |
|---|---|
| `__LABEL_DOMAIN__` | DNS domain for label and taint keys, for example `geodata.example.com`. Must match the `LABEL_DOMAIN` environment variable passed to the agent |
| `__IMAGE_REPOSITORY__` | Registry path for the agent image you build |
| `__NFS_SERVER__` | Hostname of the NFS server holding the dataset |
| `__NFS_SHARE_NAME__` | Share name, used only to build a unique volume handle |
| `__NFS_SHARE_PATH__` | Export path, for example `/data` |
| `__AMI_ALIAS__` | Pinned Bottlerocket version, for example `bottlerocket@v1.64.0` |
| `__NODE_IAM_ROLE__` | Instance profile role for Karpenter-launched nodes |
| `__SUBNET_ID_1__`, `__SUBNET_ID_2__` | Subnets Karpenter may launch into |
| `__SECURITY_GROUP_ID__` | Security group with access to the NFS export |
| `__ZONE__` | Availability zone of the source filesystem |

Check that none remain:

```bash
./scripts/check-placeholders.sh
```

Build and push the agent image, then set `__IMAGE_REPOSITORY__` to match:

```bash
cd agent
docker build --platform linux/amd64 -t geodata-sync-agent:0.4.0 .
docker tag geodata-sync-agent:0.4.0 <registry>/geodata-sync-agent:0.4.0
docker push <registry>/geodata-sync-agent:0.4.0
```

## Apply order

Numeric order is apply order.

```bash
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/01-rbac.yaml
kubectl apply -f manifests/02-ec2nodeclass.yaml
kubectl apply -f manifests/03-nodepool.yaml
kubectl apply -f manifests/04-warm-capacity.yaml
kubectl apply -f manifests/05-source-pv.yaml
kubectl apply -f manifests/06-source-pvc.yaml
kubectl apply -f manifests/07-sync-agent-daemonset.yaml
kubectl apply -f manifests/08-path-shim.yaml
```

Two of these deserve a pause.

**After `04-warm-capacity.yaml`**, confirm nodes appear. The placeholder
Deployment is what causes Karpenter to launch them; without it the pool sits at
zero.

**Before `09-local-pv.yaml`**, wait for the agent to complete a first pass on
every node. The volume's path must already exist on the node:

```bash
kubectl logs -n geodata-sync -l app=geodata-sync-agent -f
kubectl apply -f manifests/09-local-pv.yaml
kubectl get pv geodata-local-pv
```

It must read `Available` before you register the data store.

## Registering the data stores

Geocoding and routing use different mechanisms, and both are registered.

**Geocoding** uses a local PersistentVolume, registered through Enterprise
Manager. Form values are listed at the top of
`manifests/09-local-pv.yaml` and must agree with the volume spec. For the
general reference on PV-based folder data stores, see
[../PVsAsDataStores/README.md](../PVsAsDataStores/README.md).

**Routing** uses a hostPath folder data store, which Enterprise Manager cannot
create. Register it through the Admin REST API, with the shim DaemonSet Ready
first. The call and its arguments are documented at the top of
`manifests/08-path-shim.yaml`.

Registering the hostPath store is not a scoped operation. ArcGIS adds the mount
to service Deployments organisation-wide, so every node that may run any ArcGIS
service pod must have the path present. Read
[tuning/gotchas.md](tuning/gotchas.md) before you register.

## Verifying

Confirm the dataset is on the instance store rather than on EBS. Check from a
pod that mounts the path; `kubectl debug node/...` has its own mount namespace
and will report the wrong filesystem:

```bash
kubectl exec -n geodata-sync ds/geodata-path-shim -c keep-path -- df -h /mnt/geodata
```

Confirm every node in the pool holds the same generation before publishing a
service against it:

```bash
kubectl get nodes -l __LABEL_DOMAIN__/workload=geodata \
  -o custom-columns='NAME:.metadata.name,GEN:.metadata.labels.__LABEL_DOMAIN__/dataset-generation'
```

Confirm the gate has opened. Only the permanent taint should remain:

```bash
kubectl get nodes -l __LABEL_DOMAIN__/workload=geodata \
  -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints[*].key'
```

## Replacing a node

Nodes in this pool are never replaced automatically. The disruption budget is
zero and expiry is disabled, so an AMI or configuration change marks nodes
Drifted and leaves them running. Replacement is deliberate:

```bash
kubectl get nodeclaim -l karpenter.sh/nodepool=geodata-nodes \
  -o custom-columns=NAME:.metadata.name,DRIFTED:'.status.conditions[?(@.type=="Drifted")].status'
```

Replace one node at a time. Cordon it, let its workload move, then delete its
NodeClaim. Do not cordon the whole pool at once, or Karpenter provisions an
additional node. Expect a full sync on the replacement before it accepts
service pods.

## Not covered

**Automated placement after publishing.** Service pods need the pool's node
affinity and toleration, set in Enterprise Manager after publishing. This
sample does not automate that.

**Multiple concurrent generations.** The design assumes datasets are immutable
and added rather than modified. A new release is a new directory and a republish
of the dependent service.

**Multi-zone pools.** The example pins the pool to the zone holding the source
filesystem. If your filesystem is multi-AZ, list both zones and re-read the
reasoning in `manifests/03-nodepool.yaml`.

**Scoping the agent's node permissions.** The agent holds a cluster-wide
permission to patch nodes, because Kubernetes offers no way to scope node access
to the running node. See the comment in `manifests/01-rbac.yaml` and treat it as
a trust boundary to review.

## Tuning notes

- [RESULTS.md](RESULTS.md) — what was measured, on what, and what to re-measure
- [tuning/ontap.md](tuning/ontap.md) — sizing the source filesystem, and why
  IOPS rather than throughput sets the ceiling
- [tuning/nvme.md](tuning/nvme.md) — binding instance-store NVMe on Bottlerocket
- [tuning/rclone.md](tuning/rclone.md) — copy engine settings, and three flags
  that carry correctness
- [tuning/gotchas.md](tuning/gotchas.md) — traps worth knowing before you hit
  them

## Official documentation

- ArcGIS Enterprise on Kubernetes folder data stores:
  <https://enterprise-k8s.arcgis.com/en/latest/administer/system-managed-data-stores.htm>
- Karpenter: <https://karpenter.sh>
- Karpenter for ArcGIS Enterprise: [../Guides/Karpenter/README.md](../Guides/Karpenter/README.md)
- Bottlerocket ephemeral storage:
  <https://bottlerocket.dev/en/os/latest/api/settings/bootstrap-containers/>

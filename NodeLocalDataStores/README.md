# Node-local data stores for routing and geocoding

Routing and geocoding services read large file-based datasets. Served over a
network, for example, NFS, SMB, or network-attached storage (e.g., EBS, Azure Disk),
that read path is slower than reading from the node's own disk. This guide stages a
copy of the dataset onto each node in a pool dedicated just to geocoding and routing
workloads, using instance-storage instead of network storage, and blocks traffic to 
the node until the copy is complete, and registers it with ArcGIS Enterprise as a 
folder data store.

This process has been tested by copying roughly 500 GB onto two nodes in about five minutes. 
Sample code and manifests are provided as-is and unsupported.

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

Use this for provisioning large, read-only, file-based locators and network dataset
that change infrequently. A network dataset with quarterly releases is the case it
was built for.

It is a poor fit when the data changes frequently, because every change means
staging a fresh copy onto every node in the pool. It is unnecessary when the
dataset is small enough that a network share keeps up.

## How it works

A DaemonSet runs on a dedicated node pool. On each pass it copies any dataset
the node does not yet hold from an NFS export onto the node's local drive.

Three properties make it safe to point a production service at:

**Data appears atomically.** Each dataset is copied into a staging directory and
moved into place with a rename. A service never sees a partially transferred
dataset, because the rename is atomic within a filesystem and the staging
directory sits on the same one.

**Nodes are protected until they hold data.** The pool applies a startup taint at
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

- A Kubernetes cluster deployed on AWS Elastic Kubernetes Service
- ArcGIS Enterprise on Kubernetes (EKS) 12.0 or later
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
  security group. This was tested with FSx for NetApp ONTAP but another NFS share 
  with sufficient throughput and IOPS should also work.
- Amazon Elastic Container Registry (ECR) to host the agent container image
- A Docker environment, .e.g., Docker Desktop, to build the agent container image 

## Configuration

Every environment-specific value is a `__PLACEHOLDER__` token. Replace all of
them before applying anything.

| Placeholder | Meaning |
|---|---|
| `__LABEL_DOMAIN__` | DNS domain for label and taint keys, for example `geodata.example.com`. Must match the `LABEL_DOMAIN` environment variable passed to the agent |
| `__IMAGE_REPOSITORY__` | Container registry path for the agent image you build |
| `__NFS_SERVER__` | Hostname of the NFS server holding the dataset |
| `__NFS_SHARE_NAME__` | Share name, used only to build a unique volume handle |
| `__NFS_SHARE_PATH__` | Export path, for example `/data` |
| `__AMI_ALIAS__` | Pinned Bottlerocket version, for example `bottlerocket@v1.64.0` |
| `__NODE_IAM_ROLE__` | Instance profile role for Karpenter-launched nodes |
| `__SUBNET_TAG_KEY__`, `__SUBNET_TAG_VALUE__` | Tag identifying the subnets Karpenter may launch into, for example `karpenter.sh/discovery` and your cluster name. Selects any number of subnets, so one subnet or several needs no structural change. `02-ec2nodeclass.yaml` carries a commented ID-based alternative |
| `__SECURITY_GROUP_ID__` | Security group with access to the NFS export |
| `__ZONE__` | Availability zone to launch into. One zone is the default. Adding zones is a deliberate cost-for-redundancy trade, and a multi-AZ source does not make it free; read the reasoning in `03-nodepool.yaml` first |

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

Two of these steps take several minutes to complete:

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

It must read `Available` before you proceed with the remaining steps.

Once the previous steps are complete, you will have a separated, dedicate node 
pool called `geodata-nodes` and your data will have been copied from the NFS 
share onto each node at /mnt/geodata/active. Your nodes will be tainted with 
`__LABEL_DOMAIN__/dedicated: geocoding-and-routing`. If the data synced 
successfully, the data-not-ready taint should no longer apply to those nodes.

All nodes in the default node pool will also now have an empty directory 
at /mnt/geodata/active.

The remaining steps are ArcGIS-specific.

## Registering the data stores

First, we need to tell ArcGIS Enterprise where the data resides by registering
the location as a folder data store. The process is different for geocoding locators and 
network datasets:

**Geocoding** uses a local PersistentVolume, registered through ArcGIS Enterprise
Manager. Form values are listed at the top of `manifests/09-local-pv.yaml` 
and must agree with the volume spec. For the general reference on PV-based 
folder data stores, see
[../PVsAsDataStores/README.md](../PVsAsDataStores/README.md).

**Routing** for ArcGIS Enterprise on Kubernetes 12.0 12.1 uses a hostPath
folder data store, which Enterprise Manager cannot create. Register it
through the Admin REST API, with the shim DaemonSet Ready first. The call 
and its arguments are documented at the top of `manifests/08-path-shim.yaml`.

Registering the hostPath store is not a scoped operation. ArcGIS adds the mount
to service Deployments organisation-wide, so every node that may run any ArcGIS
service pod must have the path present. Read [tuning/gotchas.md](tuning/gotchas.md) 
before you register.

## Rescheduling the publishing tool pods

At this point, publishing a service that relies on data residing on the dedicated
routing and geocoding nodes will still fail because the ArcGIS Enterprise PublishingTools
system service will validate that it can access the required data before publishing
a service, and all system services run on the default node group instead. As a result, 
we need to make sure the pods hosting the PublishingTools system service runs always on the 
dedidcated node group. You can do this by applying node affinity
and tolerations to the service in ArcGIS Enterprise Manager. Doing so will automatically 
reschedule any PublishingTools pods on the routing and geocoding nodes.

## Setting the default placement properties for Map and GP services
ArcGIS Enterprise on Kubernetes does not currently support specifying node placement properties
for a service when publishing the service. They can only be set after a service has been published.
Consequently, pods hosting geocoding and routing services that rely on node-local data will be 
scheduled to run on the default node group because they do not specify the node affinity and tolerations
required to run on the geocoding and routing node group.

For most types of services, that's not a big problem as the service will publish but fail to start
because its data is missing on the default node group's nodes. The fix is to set the placement properties 
for that service in ArcGIS Enterprise Manager after publishing and to start the service. 

For routing services that are published via the 
[enterprise portal](https://doc.esri.com/en/arcgis-enterprise/latest/administer/configure-routing-services.html?pivots=os-windows#6F5)
or through the 
[Publish Routing Services](https://developers.arcgis.com/rest/services-reference/enterprise/publish-routing-services/)
server tool, this process will fail to publish altogether and requires an additional workaround.

That workaround is to edit the default placement properties for the MapServer, GPServer 
and GPServerSync server types that use the ArcObjects11 provider. This has to be performed
using the ArcGIS Enterprise Admin REST API by editing the System > Deployment >
[Default Deployment Properties](https://developers.arcgis.com/rest/enterprise-administration/enterprise/deployment-default-properties/).

The API documentation has an example of how to configure pod placement properties. Here's an example of how to insert a pod placement
policy into the default deployment properties for the MapServer/ArcObjects11 type and provider. Note that `__LABEL_DOMAIN__  is a placeholder
to be replace with your specific configuration value, e.g., `geodata.example.com`.

```json
{
  "mode": "Dedicated",
  "provider": "ArcObjects11",
  "id": "<id>",
  "type": "MapServer",
  "spec": {
    "replicas": {
      "min": 1,
      "max": 1,
      "scalingMode": "manual"
    },
    "podPlacementPolicy": {
      "tolerations": [{
        "effect": "NoSchedule",
        "value": "geocoding-and-routing",
        "key": "__LABEL_DOMAIN__/dedicated",
        "operator": "Equal"
      }],
      "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{
        "values": ["geocoding-and-routing"],
        "key": "__LABEL_DOMAIN__/workload",
        "operator": "In"
      }]}]}}
    },
    "containers": [
      {
        "name": "main-container",
        "resources": {
          "memoryMin": "500Mi",
          "memoryMax": "2Gi",
          "cpuMin": "0.125",
          "customResources": {},
          "cpuMax": "2"
        },
        "containerImageKey": "MAP_SERVER"
      },
      {
        "name": "fluent-bit",
        "resources": {
          "memoryMin": "32Mi",
          "memoryMax": "150Mi",
          "cpuMin": "0.05",
          "customResources": {},
          "cpuMax": "0.25"
        },
        "containerImageKey": "FLUENT_BIT"
      }
    ]
  },
  "revision": <revision>
}

```

Repeat this for GPServer and GPServerSync types that use the ArcObjects11 provider as well. 

## Publishing routing and geocoding services

## Verifying

Confirm the dataset is on the instance store rather than on EBS. Check from a
pod that mounts the path; `kubectl debug node/...` has its own mount namespace
and will report the wrong filesystem.

THE POD MUST BE ONE RUNNING ON A NODE IN THE POOL, which is why this does not
address `ds/geodata-path-shim`. `kubectl exec ds/<name>` picks an arbitrary pod
from the set, and the shim runs on every node in the cluster on purpose, so it
usually lands on a node that holds no data and reports that node's EBS volume.
That is the same wrong answer as `kubectl debug node/...`, arrived at a
different way. Use the sync agent instead, which only ever schedules onto nodes
carrying the pool label:

```bash
kubectl exec -n geodata-sync ds/geodata-sync-agent -- df -hT /mnt/data
```

The agent mounts the same host path at `/mnt/data` rather than at
`/mnt/geodata`; see the volumeMounts in `07-sync-agent-daemonset.yaml`. Expect
an instance-store device, and an `md` array where the instance type carries more
than one NVMe disk:

```
Filesystem     Type  Size  Used Avail Use% Mounted on
/dev/md127     xfs   2.6T  556G  2.1T  21% /mnt/data
```

A size matching the EBS data volume in `02-ec2nodeclass.yaml`, 300G, means the
bind did not happen and the dataset went to the wrong device.

Separately, confirm the shim did its job on a node OUTSIDE the pool, since that
is what makes a hostPath data store mountable organisation-wide. The path should
exist, be empty, and sit on that node's EBS volume:

```bash
NODE=$(kubectl get nodes -l '__LABEL_DOMAIN__/workload!=geodata' \
  -o jsonpath='{.items[0].metadata.name}')
POD=$(kubectl get pods -n geodata-sync -l app=geodata-path-shim \
  --field-selector spec.nodeName=$NODE -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n geodata-sync "$POD" -c keep-path -- df -hT /mnt/geodata
```

Confirm every node in the pool holds the same generation before publishing a
service against it.

ESCAPE THE DOTS in the label key on the `custom-columns` side. That argument is
JSONPath, where an unescaped dot is a field separator, so a domain-qualified key
substituted in as-is silently resolves to nothing and every row reads `<none>`.
The `-l` selector needs no escaping, only the JSONPath does:

```bash
kubectl get nodes -l __LABEL_DOMAIN__/workload=geodata \
  -o custom-columns='NAME:.metadata.name,GEN:.metadata.labels.__LABEL_DOMAIN__/dataset-generation'
```

So with a label domain of `geodata.example.com`, the second line reads
`.metadata.labels.geodata\.example\.com/dataset-generation`.

Confirm that routing and geocoding pods can be scheduled on the nodes. 
Only the permanent taint should remain:

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
NodeClaim. If you cordon the whole pool at once, Karpenter will provision one or more
additional nodes. Expect a full sync on the replacement before it accepts
service pods.

## Not covered

**Automated placement after publishing.** Service pods need the pool's node
affinity and toleration, set in Enterprise Manager after publishing. This
sample does not automate that.

**Multiple concurrent generations.** The design assumes datasets are immutable
and added rather than modified. A new release is a new directory and a republish
of the dependent service.

**A tested multi-zone pool.** Adding zones is documented in
`manifests/03-nodepool.yaml`, including why a multi-AZ source filesystem does not
make a second zone free. Single-zone is the arrangement that was measured; a
multi-zone pool has not been run end to end here.

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

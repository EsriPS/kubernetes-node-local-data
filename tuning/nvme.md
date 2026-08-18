# Putting the dataset on instance-store NVMe

Bind the instance store to a path under `/mnt/`, and do it from a single
bootstrap entry. Both halves of that sentence are constraints rather than
preferences, and getting either wrong produces a node that looks healthy while
serving reads from the wrong device.

This note covers Bottlerocket on Karpenter. The equivalent on AL2023 is a
userData script that formats and mounts the instance store itself.

## Why instance store rather than EBS

Instance-store NVMe is the read path the services use once the data is staged.
It changes the sync time very little, because the sync is bound by the source
filesystem rather than by the destination disk, so judge it on read performance
and not on how fast the first copy completes.

The cost is that instance-store data is lost on a stop or a hardware
replacement, not only on termination. That is what makes the bootstrap gate and
its re-application necessary; see [gotchas.md](gotchas.md).

## The bind target must be under /mnt/

Bottlerocket allow-lists ephemeral-storage bind targets. The set is five named
Kubernetes directories plus anything under `/mnt/`. Anything else is rejected
with "specified bind directory not in allow list".

`/var/lib/<anything>` is not on the list. Check the allow-list on your AMI
before choosing a path:

```bash
apiclient ephemeral-storage list-dirs
```

Choosing the path first and discovering the allow-list later is expensive,
because the path appears in the DaemonSet, the shim, the local PersistentVolume
and the registered data store. A PersistentVolume's source is immutable, so
moving it means deregistering the data store, deleting the volume and
re-registering.

## Both commands must be in one bootstrap entry

```toml
[settings.bootstrap-commands.geodata-nvme]
commands = [["apiclient", "ephemeral-storage", "init"], ["apiclient", "ephemeral-storage", "bind", "--dirs", "/mnt/geodata"]]
mode = "always"
essential = true
```

Written as two entries, this fails in a way that leaves no error behind.
Karpenter serialises the userData TOML map alphabetically, so an entry named for
`bind` is written ahead of one named for `init` and runs first. `bind` returns
success when ephemeral storage is not yet initialised: it logs that it is
skipping and exits zero. Nothing fails, the boot completes, the node joins, and
the dataset lands on EBS.

Commands inside one entry run in the order given, which no re-serialisation can
disturb.

`essential = true` is the point rather than an oversight. A rejected bind must
halt the boot. The alternative is a node that joins, syncs several hundred
gigabytes onto EBS, and reports read performance for the wrong device.

## Do not set instanceStorePolicy alongside this

`instanceStorePolicy: RAID0` makes Karpenter emit its own ephemeral-storage
bootstrap commands, which bind kubelet, containerd and pod logs, and never an
arbitrary path. Setting it alongside the commands above means both call `init`
with no ordering guarantee between them.

Leaving it unset also keeps Karpenter's allocatable ephemeral-storage accounting
honest, since kubelet and containerd then stay on the EBS data volume, which is
what that accounting measures.

## Size the instance store for two generations

The agent stages a new dataset beside the active one and renames it into place,
so a node holds two full copies at the moment of a changeover. Require the
instance store accordingly:

```yaml
- key: karpenter.k8s.aws/instance-local-nvme
  operator: Gte
  values: ["1400"]
```

Expressing this as a floor rather than a pinned instance type lets Karpenter
choose from a mixed fleet. Constrain instance category as well, otherwise the
floor alone admits GPU and FPGA types that happen to carry large instance
stores.

A single-device instance type is mounted directly and builds no RAID array, so
`/dev/md` will not exist. That is expected. Types with several devices are
striped into one array by the same command.

## Verify from a pod, not from kubectl debug

`kubectl debug node/...` gets its own mount namespace and does not see the
host's mounts, so it will report the wrong filesystem and look authoritative
doing it. Check from a pod that hostPath-mounts the path:

```bash
kubectl exec -n geodata-sync ds/geodata-path-shim -c keep-path -- df -h /mnt/geodata
```

The filesystem must be the instance store, not the EBS data volume. Keeping the
EBS data volume too small to hold the dataset is a second guard: if the bind
silently failed, the sync runs out of space rather than succeeding against the
wrong device.

A rejected bind halts the boot, so the symptom is an instance that runs, passes
both EC2 status checks, and never appears in `kubectl get nodes`. That symptom
is identical to a missing subcommand, which is worth knowing before diagnosing
it.

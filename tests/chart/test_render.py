"""Focused example-based render and validation tests for the Helm chart."""

from collections import Counter
from pathlib import Path
import json
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHART_PATH = REPOSITORY_ROOT / "charts" / "node-local-data"
EXAMPLE_VALUES = REPOSITORY_ROOT / "examples" / "values-example.yaml"
INSTANCE_TYPE_VALUES = REPOSITORY_ROOT / "examples" / "values-instance-types.yaml"
PLACEHOLDER_PATTERN = re.compile(r"__[A-Z0-9_]+__")

Resource = Dict[str, object]


def resource(
    manifests: List[Resource], kind: str, name: Optional[str] = None
) -> Resource:
    """Return the unique rendered resource matching kind and optional name."""
    matches = [
        item
        for item in manifests
        if item["kind"] == kind
        and (name is None or item["metadata"]["name"] == name)
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def keyed_resources(manifests: List[Resource]) -> Dict[Tuple[str, str], Resource]:
    """Key rendered resources by their stable Kubernetes identity."""
    return {
        (item["kind"], item["metadata"]["name"]): item for item in manifests
    }


def assert_no_placeholders(manifests: List[Resource], notes: str) -> None:
    rendered = yaml.safe_dump_all(manifests, sort_keys=True) + notes
    assert PLACEHOLDER_PATTERN.search(rendered) is None


def helm_error(error: pytest.ExceptionInfo[subprocess.CalledProcessError]) -> str:
    return f"{error.value.stdout or ''}\n{error.value.stderr or ''}"


def test_documented_example_renders_default_stage_and_core_contract(render_chart):
    manifests, notes = render_chart(EXAMPLE_VALUES)

    assert Counter(item["kind"] for item in manifests) == Counter(
        {
            "Namespace": 1,
            "ServiceAccount": 1,
            "ClusterRole": 1,
            "ClusterRoleBinding": 1,
            "EC2NodeClass": 1,
            "NodePool": 1,
            "PriorityClass": 1,
            "Deployment": 1,
            "DaemonSet": 2,
            "StorageClass": 1,
            "PersistentVolume": 1,
            "PersistentVolumeClaim": 1,
        }
    )

    service_account = resource(manifests, "ServiceAccount", "geodata-sync-agent")
    cluster_role = resource(manifests, "ClusterRole", "geodata-sync-agent")
    binding = resource(manifests, "ClusterRoleBinding", "geodata-sync-agent")
    sync_agent = resource(manifests, "DaemonSet", "geodata-sync-agent")
    source_sc = resource(manifests, "StorageClass", "geodata-source-sc")
    source_pv = resource(manifests, "PersistentVolume", "geodata-source-pv")
    source_pvc = resource(
        manifests, "PersistentVolumeClaim", "geodata-source-pvc"
    )
    node_class = resource(manifests, "EC2NodeClass", "geodata-nodes")
    node_pool = resource(manifests, "NodePool", "geodata-nodes")

    assert service_account["metadata"]["namespace"] == "geodata-sync"
    assert binding["roleRef"]["name"] == cluster_role["metadata"]["name"]
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": service_account["metadata"]["name"],
            "namespace": "geodata-sync",
        }
    ]
    assert cluster_role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["nodes"],
            "verbs": ["get", "list", "watch", "patch"],
        }
    ]

    assert source_sc["reclaimPolicy"] == "Retain"
    assert source_pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert source_pv["spec"]["storageClassName"] == source_sc["metadata"]["name"]
    assert source_pv["spec"]["claimRef"] == {
        "namespace": "geodata-sync",
        "name": source_pvc["metadata"]["name"],
    }
    assert source_pvc["spec"]["storageClassName"] == source_sc["metadata"]["name"]
    assert source_pvc["spec"]["volumeName"] == source_pv["metadata"]["name"]
    assert source_pv["spec"]["capacity"]["storage"] == "1536Gi"
    assert source_pvc["spec"]["resources"]["requests"]["storage"] == "1536Gi"
    assert source_pv["spec"]["csi"]["volumeHandle"] == (
        "10.0.0.10#geodata#geodata-source-pv"
    )
    assert sync_agent["spec"]["template"]["spec"]["serviceAccountName"] == (
        service_account["metadata"]["name"]
    )
    assert sync_agent["spec"]["template"]["spec"]["volumes"][0][
        "persistentVolumeClaim"
    ]["claimName"] == source_pvc["metadata"]["name"]
    assert sync_agent["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
        "/geodata-sync-agent:0.5.0"
    )

    assert node_pool["spec"]["template"]["spec"]["nodeClassRef"]["name"] == (
        node_class["metadata"]["name"]
    )
    assert node_pool["spec"]["limits"] == {"nodes": "3", "cpu": 144}
    template_spec = node_pool["spec"]["template"]["spec"]
    assert template_spec["expireAfter"] == "Never"
    assert "terminationGracePeriod" not in template_spec
    assert node_pool["spec"]["disruption"] == {
        "consolidationPolicy": "WhenEmpty",
        "consolidateAfter": "Never",
        "budgets": [{"nodes": "0"}],
    }

    data_volumes = node_class["spec"]["blockDeviceMappings"]
    assert data_volumes[1]["ebs"]["volumeSize"] == "200Gi"
    bootstrap = node_class["spec"]["userData"]
    assert bootstrap.index('"init"') < bootstrap.index('"bind"')
    assert '"--dirs", "/mnt/geodata"' in bootstrap

    sync_pod = sync_agent["spec"]["template"]["spec"]
    sync_container = sync_pod["containers"][0]
    assert "--dest-root=/mnt/geodata" in sync_container["args"]
    assert sync_container["volumeMounts"][1]["mountPath"] == "/mnt/geodata"
    assert sync_pod["volumes"][1]["hostPath"]["path"] == "/mnt/geodata"

    # The local PV, its StorageClass, and the whole ``localVolume`` values
    # surface are removed, so the notes advertise no local-volume registration
    # and no second-stage enablement upgrade. What remains is one shared
    # hostPath registration serving both routing and geocoding.
    assert "localVolume" not in notes
    assert "Local storage is disabled" not in notes
    assert "Local-volume registration values" not in notes
    assert "completed its first sync" in notes
    assert "Shared hostPath registration" in notes
    assert "Host path: /mnt/geodata" in notes
    assert "for both routing and geocoding" in notes
    assert "data.example.com/workload=geodata" in notes
    assert "daemonset/geodata-sync-agent" in notes
    assert_no_placeholders(manifests, notes)

    for item in manifests:
        annotations = item["metadata"].get("annotations", {})
        assert "helm.sh/resource-policy" not in annotations
        assert "helm.sh/hook" not in annotations


def test_instance_type_example_replaces_default_floor_requirements(render_chart):
    manifests, notes = render_chart(INSTANCE_TYPE_VALUES)
    node_pool = resource(manifests, "NodePool", "geodata-nodes")
    requirements = node_pool["spec"]["template"]["spec"]["requirements"]

    assert [entry["key"] for entry in requirements[:4]] == [
        "kubernetes.io/arch",
        "kubernetes.io/os",
        "topology.kubernetes.io/zone",
        "karpenter.sh/capacity-type",
    ]
    assert requirements[4:] == [
        {
            "key": "karpenter.k8s.aws/instance-type",
            "operator": "In",
            "values": ["r8id.8xlarge"],
        }
    ]
    assert not any(
        entry["key"].startswith("karpenter.k8s.aws/instance-")
        and entry["key"] != "karpenter.k8s.aws/instance-type"
        for entry in requirements
    )
    assert_no_placeholders(manifests, notes)


def test_warm_capacity_disabled_omits_both_resources_and_gates_notes(render_chart):
    manifests, notes = render_chart(
        EXAMPLE_VALUES, set_values=("warmCapacity.enabled=false",)
    )

    assert len(manifests) == 11
    assert not any(item["kind"] == "PriorityClass" for item in manifests)
    assert not any(item["kind"] == "Deployment" for item in manifests)
    assert resource(manifests, "DaemonSet", "geodata-sync-agent")
    assert resource(manifests, "DaemonSet", "geodata-path-shim")
    assert "Warm capacity is disabled" in notes
    assert "must wait for" in notes
    assert_no_placeholders(manifests, notes)


def test_path_shim_disabled_changes_only_path_shim_resource(render_chart):
    baseline, baseline_notes = render_chart(EXAMPLE_VALUES)
    disabled, disabled_notes = render_chart(
        EXAMPLE_VALUES, set_values=("pathShim.enabled=false",)
    )

    baseline_by_id = keyed_resources(baseline)
    path_shim_id = ("DaemonSet", "geodata-path-shim")
    assert path_shim_id in baseline_by_id
    baseline_by_id.pop(path_shim_id)
    assert keyed_resources(disabled) == baseline_by_id
    assert "Shared hostPath registration" in baseline_notes
    assert "Host path: /mnt/geodata" in baseline_notes
    assert "for both routing and geocoding" in baseline_notes
    assert "Shared hostPath registration" not in disabled_notes
    assert "Host path:" not in disabled_notes
    assert_no_placeholders(disabled, disabled_notes)


def test_namespace_creation_disabled_uses_configured_namespace_everywhere(render_chart):
    manifests, notes = render_chart(
        EXAMPLE_VALUES,
        set_values=("namespace.create=false", "namespace.name=existing-sync"),
    )

    assert len(manifests) == 12
    assert not any(item["kind"] == "Namespace" for item in manifests)
    namespaced_kinds = {
        "ServiceAccount",
        "Deployment",
        "DaemonSet",
        "PersistentVolumeClaim",
    }
    for item in manifests:
        if item["kind"] in namespaced_kinds:
            assert item["metadata"]["namespace"] == "existing-sync"

    binding = resource(manifests, "ClusterRoleBinding", "geodata-sync-agent")
    source_pv = resource(manifests, "PersistentVolume", "geodata-source-pv")
    assert binding["subjects"][0]["namespace"] == "existing-sync"
    assert source_pv["spec"]["claimRef"]["namespace"] == "existing-sync"
    assert "kubectl --namespace existing-sync" in notes
    assert "--namespace existing-sync" in notes
    assert_no_placeholders(manifests, notes)


def test_helper_derived_identifiers_agree_across_resources_and_notes(render_chart):
    """Validates Requirements 1.5, 9.3, and 9.9."""
    manifests, notes = render_chart(
        EXAMPLE_VALUES,
        set_values=(
            "namePrefix=consistency",
            "namespace.name=consistency-sync",
            "labelDomain=consistency.example.com",
            "workloadValue=mirror",
        ),
    )

    namespace = resource(manifests, "Namespace", "consistency-sync")
    service_account = resource(
        manifests, "ServiceAccount", "consistency-sync-agent"
    )
    cluster_role = resource(manifests, "ClusterRole", "consistency-sync-agent")
    binding = resource(
        manifests, "ClusterRoleBinding", "consistency-sync-agent"
    )
    node_class = resource(manifests, "EC2NodeClass", "consistency-nodes")
    node_pool = resource(manifests, "NodePool", "consistency-nodes")
    priority_class = resource(
        manifests, "PriorityClass", "consistency-warm-capacity"
    )
    warm_deployment = resource(
        manifests, "Deployment", "consistency-warm-capacity"
    )
    sync_agent = resource(manifests, "DaemonSet", "consistency-sync-agent")
    path_shim = resource(manifests, "DaemonSet", "consistency-path-shim")
    source_sc = resource(
        manifests, "StorageClass", "consistency-source-sc"
    )
    source_pv = resource(
        manifests, "PersistentVolume", "consistency-source-pv"
    )
    source_pvc = resource(
        manifests, "PersistentVolumeClaim", "consistency-source-pvc"
    )

    namespaced_resources = (
        service_account,
        warm_deployment,
        sync_agent,
        path_shim,
        source_pvc,
    )
    assert all(
        item["metadata"]["namespace"] == namespace["metadata"]["name"]
        for item in namespaced_resources
    )
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": service_account["metadata"]["name"],
            "namespace": namespace["metadata"]["name"],
        }
    ]
    assert source_pv["spec"]["claimRef"]["namespace"] == (
        namespace["metadata"]["name"]
    )
    assert "kubectl --namespace consistency-sync" in notes
    assert "--namespace consistency-sync" in notes

    assert binding["roleRef"]["name"] == cluster_role["metadata"]["name"]
    assert binding["metadata"]["name"] == service_account["metadata"]["name"]
    assert sync_agent["metadata"]["name"] == service_account["metadata"]["name"]
    assert sync_agent["spec"]["template"]["spec"]["serviceAccountName"] == (
        service_account["metadata"]["name"]
    )
    assert "daemonset/consistency-sync-agent" in notes

    assert node_pool["metadata"]["name"] == node_class["metadata"]["name"]
    assert node_pool["spec"]["template"]["spec"]["nodeClassRef"]["name"] == (
        node_class["metadata"]["name"]
    )
    assert "[settings.bootstrap-commands.consistency-nvme]" in node_class[
        "spec"
    ]["userData"]

    warm_name = priority_class["metadata"]["name"]
    warm_selector = {"app": warm_name}
    assert warm_deployment["metadata"]["name"] == warm_name
    assert warm_deployment["metadata"]["labels"]["app"] == warm_name
    assert warm_deployment["spec"]["selector"]["matchLabels"] == warm_selector
    assert warm_deployment["spec"]["template"]["metadata"]["labels"] == (
        warm_selector
    )
    warm_pod = warm_deployment["spec"]["template"]["spec"]
    assert warm_pod["priorityClassName"] == warm_name
    assert warm_pod["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ][0]["labelSelector"]["matchLabels"] == warm_selector

    for daemon_set in (sync_agent, path_shim):
        daemon_name = daemon_set["metadata"]["name"]
        expected_selector = {"app": daemon_name}
        assert daemon_set["spec"]["selector"]["matchLabels"] == (
            expected_selector
        )
        assert daemon_set["spec"]["template"]["metadata"]["labels"][
            "app"
        ] == daemon_name

    assert source_pv["spec"]["storageClassName"] == source_sc["metadata"][
        "name"
    ]
    assert source_pvc["spec"]["storageClassName"] == source_sc["metadata"][
        "name"
    ]
    assert source_pvc["spec"]["volumeName"] == source_pv["metadata"]["name"]
    assert source_pv["spec"]["claimRef"]["name"] == source_pvc["metadata"][
        "name"
    ]
    assert sync_agent["spec"]["template"]["spec"]["volumes"][0][
        "persistentVolumeClaim"
    ]["claimName"] == source_pvc["metadata"]["name"]
    assert source_pv["spec"]["csi"]["volumeHandle"] == (
        f"10.0.0.10#geodata#{source_pv['metadata']['name']}"
    )

    component_key = "consistency.example.com/component"
    common_labeled_resources = (
        namespace,
        service_account,
        cluster_role,
        binding,
        warm_deployment,
        sync_agent,
        path_shim,
        source_sc,
        source_pv,
        source_pvc,
    )
    assert all(
        item["metadata"]["labels"][component_key] == "node-local-data-stores"
        for item in common_labeled_resources
    )
    assert node_class["spec"]["tags"][component_key] == (
        "node-local-data-stores"
    )
    assert sync_agent["spec"]["template"]["metadata"]["labels"][
        component_key
    ] == "node-local-data-stores"
    assert path_shim["spec"]["template"]["metadata"]["labels"][
        component_key
    ] == "node-local-data-stores"

    workload_key = "consistency.example.com/workload"
    assert node_pool["spec"]["template"]["metadata"]["labels"] == {
        workload_key: "mirror"
    }
    assert warm_pod["nodeSelector"] == {workload_key: "mirror"}
    assert sync_agent["spec"]["template"]["spec"]["nodeSelector"] == {
        workload_key: "mirror"
    }
    assert "--selector consistency.example.com/workload=mirror" in notes
    assert "- key: consistency.example.com/workload" in notes

    dedicated_key = "consistency.example.com/dedicated"
    not_ready_key = "consistency.example.com/data-not-ready"
    node_pool_spec = node_pool["spec"]["template"]["spec"]
    assert node_pool_spec["taints"][0]["key"] == dedicated_key
    assert node_pool_spec["startupTaints"][0]["key"] == not_ready_key
    for pod_spec in (
        warm_pod,
        sync_agent["spec"]["template"]["spec"],
        path_shim["spec"]["template"]["spec"],
    ):
        assert {entry["key"] for entry in pod_spec["tolerations"]} == {
            dedicated_key,
            not_ready_key,
        }
    assert f"- key: {dedicated_key}" in notes
    assert f'@.key=="{not_ready_key}"' in notes
    assert (
        "GENERATION:.metadata.labels.consistency\\.example\\.com/"
        "dataset-generation"
    ) in notes
    assert (
        "COUNT:.metadata.labels.consistency\\.example\\.com/dataset-count"
    ) in notes


def test_source_pv_and_pvc_share_configured_capacity(render_chart):
    """Validates Requirements 4.2, 9.3, and 9.9."""
    manifests, _ = render_chart(
        EXAMPLE_VALUES, set_values=("nfs.capacity=2048Gi",)
    )

    source_pv = resource(manifests, "PersistentVolume", "geodata-source-pv")
    source_pvc = resource(
        manifests, "PersistentVolumeClaim", "geodata-source-pvc"
    )
    pv_capacity = source_pv["spec"]["capacity"]["storage"]
    pvc_capacity = source_pvc["spec"]["resources"]["requests"]["storage"]

    assert pv_capacity == pvc_capacity == "2048Gi"


@pytest.mark.parametrize(
    ("dataset_root", "set_values"),
    [
        ("/mnt/geodata", ()),
        (
            "/mnt/alternate-geodata",
            ("datasetRoot=/mnt/alternate-geodata",),
        ),
    ],
)
def test_dataset_paths_use_exact_configured_root(
    render_chart, dataset_root, set_values
):
    """Validates Requirements 4.4-4.5, 6.5, 8.2-8.3, 9.3, and 9.9."""
    manifests, notes = render_chart(EXAMPLE_VALUES, set_values=set_values)

    node_class = resource(manifests, "EC2NodeClass", "geodata-nodes")
    bootstrap = node_class["spec"]["userData"]
    bind_target = re.search(
        r'\["apiclient", "ephemeral-storage", "bind", "--dirs", "([^"]+)"\]',
        bootstrap,
    )
    assert bind_target is not None
    assert bind_target.group(1) == dataset_root

    sync_agent = resource(manifests, "DaemonSet", "geodata-sync-agent")
    sync_pod = sync_agent["spec"]["template"]["spec"]
    sync_container = sync_pod["containers"][0]
    destination_mount = next(
        mount
        for mount in sync_container["volumeMounts"]
        if mount["name"] == "node-local-data"
    )
    destination_volume = next(
        volume
        for volume in sync_pod["volumes"]
        if volume["name"] == "node-local-data"
    )
    assert destination_mount["mountPath"] == dataset_root
    assert destination_volume["hostPath"]["path"] == dataset_root

    # The agent uses this single root for the direct sync destination, readiness
    # label derivation, and absent-or-empty-root taint reconciliation.
    destination_root_args = [
        argument
        for argument in sync_container["args"]
        if argument.startswith("--dest-root=")
    ]
    assert destination_root_args == [f"--dest-root={dataset_root}"]

    path_shim = resource(manifests, "DaemonSet", "geodata-path-shim")
    path_shim_pod = path_shim["spec"]["template"]["spec"]
    path_shim_volume = next(
        volume
        for volume in path_shim_pod["volumes"]
        if volume["name"] == "dataset-root"
    )
    assert path_shim_volume["hostPath"] == {
        "path": dataset_root,
        "type": "DirectoryOrCreate",
    }
    for container in (
        path_shim_pod["initContainers"][0],
        path_shim_pod["containers"][0],
    ):
        environment = {entry["name"]: entry["value"] for entry in container["env"]}
        dataset_mount = next(
            mount
            for mount in container["volumeMounts"]
            if mount["name"] == "dataset-root"
        )
        assert environment["DATASET_ROOT"] == dataset_root
        assert dataset_mount["mountPath"] == dataset_root
        assert "$DATASET_ROOT" in container["command"][-1]

    # The notes advertise a single registration path, the one shared hostPath
    # folder data store both routing and geocoding read. The former "Dataset
    # path" and "folder path is exactly" lines lived in the removed local-volume
    # branch; the shared-hostPath note replaces them.
    registration = re.search(
        r"^Shared hostPath registration for ArcGIS:\n  Host path: (.+)$",
        notes,
        re.MULTILINE,
    )
    assert registration is not None
    assert registration.group(1) == dataset_root
    assert "Register that one folder data store for both routing and geocoding" in notes
    assert set(re.findall(r"/mnt/[^\s\"']*", notes)) == {dataset_root}


@pytest.mark.parametrize(
    ("set_value", "expected_error"),
    [
        ("image.pullPolicy=Sometimes", "/image/pullPolicy"),
        ("nfs.capacity=invalid!", "/nfs/capacity"),
        ("unexpectedValue=true", "unexpectedValue"),
    ],
)
def test_schema_rejects_representative_invalid_values(
    render_chart, set_value, expected_error
):
    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, set_values=(set_value,))

    assert expected_error in helm_error(error)


@pytest.mark.parametrize(
    ("set_values", "expected_error"),
    [
        (
            ("datasetRoot=/mnt",),
            "datasetRoot must be a non-root path below /mnt/",
        ),
        (
            ("warmCapacity.replicas=4",),
            "warmCapacity.replicas must not exceed nodePool.limits.nodes",
        ),
        (
            (
                "nodePool.instanceRequirements[0].key=kubernetes.io/arch",
                "nodePool.instanceRequirements[0].operator=In",
                "nodePool.instanceRequirements[0].values[0]=amd64",
            ),
            "nodePool.instanceRequirements must not redefine chart-managed key kubernetes.io/arch",
        ),
    ],
)
def test_template_relationship_validation_rejects_invalid_values(
    render_chart, set_values, expected_error
):
    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, set_values=set_values)

    assert expected_error in helm_error(error)


# ==========================================================================
# Legacy-key rejection (2.1, 2.2)
# ==========================================================================
#
# SCOPE: this proves representability only. A real SELinux relabel cannot be
# reproduced by `helm template`, so a green run here is never proof that the
# relabel itself is fixed; that rests on the recorded live evidence.
#
# Schema validation runs before templates in Helm, so `values.schema.json`
# declares `localVolume` permissively for the sole purpose of letting a leftover
# 0.1.0 key reach the explanatory `fail` in `_validate.tpl`. These cases are what
# keep that arrangement honest: if the declaration is dropped the message
# degrades to a bare unknown-property error, and if the template check is dropped
# the key is silently accepted.


LEGACY_LOCAL_VOLUME_VALUES = """\
localVolume:
  enabled: true
  storageClassName: geodata-local-sc
  persistentVolumeName: geodata-local-pv
  capacity: 800Gi
"""


def test_legacy_local_volume_key_is_rejected_with_an_explanation(
    render_chart, tmp_path: Path
):
    """A 0.1.0 values file carrying ``localVolume`` fails with a real explanation.

    The operator must learn that the key was removed and where geocoding is
    served from instead, not just that an unknown property was supplied.

    **Validates: Requirements 2.1, 2.2**
    """
    legacy_values = tmp_path / "legacy-local-volume.yaml"
    legacy_values.write_text(LEGACY_LOCAL_VOLUME_VALUES)

    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart((EXAMPLE_VALUES, legacy_values))

    message = helm_error(error)
    assert "localVolume" in message
    assert "removed" in message
    assert "hostPath" in message
    # The point of the whole arrangement: an explanation, not a schema stub.
    assert "additional properties" not in message


@pytest.mark.parametrize(
    "set_values",
    [
        ("localVolume.enabled=true",),
        ("localVolume.enabled=false",),
        ("localVolume.capacity=800Gi",),
    ],
)
def test_legacy_local_volume_key_is_rejected_whatever_it_carries(
    render_chart, set_values
):
    """Presence of the key is the failure, not its value.

    ``localVolume.enabled=false`` is included deliberately: the setting resolves
    as removed rather than defaulted false, so an operator who merely flipped it
    off still has to delete the block.

    **Validates: Requirements 2.1, 2.2**
    """
    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, set_values=set_values)

    assert "localVolume was removed" in helm_error(error)


# ==========================================================================
# Fixed-behaviour render and schema cases (2.1, 3.8)
# ==========================================================================
#
# SCOPE: these cases prove representability only. A real SELinux relabel cannot
# be reproduced by `helm template` — it needs an enforcing Bottlerocket node, a
# relabeling volume, and a real pod, and because the label is written on disk per
# inode (D1) and cannot be relabeled back (F1), triggering one costs a node. The
# runtime behaviour is covered by the recorded live evidence in the spec
# directory. **A green chart suite is never proof that the relabel is fixed.**
#
# The legacy-key rejection itself is covered above by
# ``test_legacy_local_volume_key_is_rejected_with_an_explanation`` and
# ``test_legacy_local_volume_key_is_rejected_whatever_it_carries``; the cases
# below cover what those two do not.


def schema_document() -> Dict[str, object]:
    """The chart's values schema, read as data rather than rendered."""
    schema_path = REPOSITORY_ROOT / "charts" / "node-local-data" / "values.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_schema_declares_local_volume_permissively_as_a_pass_through():
    """``localVolume`` must stay declared, and stay unconstrained.

    Helm validates ``values.schema.json`` *before* it renders templates. With
    top-level ``additionalProperties: false`` and no declaration, a leftover
    ``localVolume`` key is rejected as a bare unknown property and the operator
    never reaches the explanation in ``_validate.tpl``. The declaration therefore
    exists to be permissive: a description and nothing else, absent from
    ``required``, so any shape of leftover block passes the schema and lands on
    the template's message.

    This pins that arrangement structurally. Reading the render error alone
    cannot distinguish "declared permissively" from "declared loosely enough for
    the values I happened to try", and the odd description-only entry is exactly
    the kind of thing a later reader tidies away.

    **Validates: Requirements 2.1, 3.8**
    """
    schema = schema_document()

    # The condition that makes the declaration necessary in the first place.
    assert schema["additionalProperties"] is False

    declaration = schema["properties"]["localVolume"]
    assert set(declaration) == {"description"}, (
        "localVolume must carry a description and no constraints; any type, "
        "properties, or additionalProperties entry can reject a leftover block "
        "at the schema and suppress the explanatory template message"
    )
    assert "REMOVED" in declaration["description"]
    assert "_validate.tpl" in declaration["description"]
    assert "localVolume" not in schema["required"]


def test_legacy_key_reaches_the_template_while_other_unknown_keys_stop_at_the_schema(
    render_chart, tmp_path: Path
):
    """The two rejection paths are asymmetric, deliberately, and must stay so.

    ``unexpectedValue`` is the same key the schema parametrize case above uses;
    it is re-rendered here only as the contrast partner. The assertion is the
    asymmetry itself, which no other case states: an unknown key stops at the
    schema and is named there, while ``localVolume`` passes the schema on purpose
    and is rejected by the template with an explanation.

    Removing the permissive schema declaration collapses the two paths into one
    and fails this. Removing top-level ``additionalProperties: false`` lets
    ``unexpectedValue`` render and also fails this.

    **Validates: Requirements 2.1, 3.8**
    """
    with pytest.raises(subprocess.CalledProcessError) as unknown_key_error:
        render_chart(EXAMPLE_VALUES, set_values=("unexpectedValue=true",))

    unknown_key_message = helm_error(unknown_key_error)
    assert "additional properties" in unknown_key_message
    assert "unexpectedValue" in unknown_key_message

    legacy_values = tmp_path / "legacy-local-volume.yaml"
    legacy_values.write_text(LEGACY_LOCAL_VOLUME_VALUES)
    with pytest.raises(subprocess.CalledProcessError) as legacy_key_error:
        render_chart((EXAMPLE_VALUES, legacy_values))

    legacy_key_message = helm_error(legacy_key_error)
    assert "additional properties" not in legacy_key_message
    assert "localVolume was removed in chart 0.2.0" in legacy_key_message
    assert "_validate.tpl" in legacy_key_message or "execution error" in (
        legacy_key_message
    ), "the rejection must come from template execution, not from the schema"


# --------------------------------------------------------------------------
# The forward overlap guard
# --------------------------------------------------------------------------
#
# The guard in ``_validate.tpl`` builds a relabeling-mount set that is EMPTY by
# construction: every host-directory mount the chart renders is a hostPath, and
# the hostPath plugin never relabels. So no values file can reach the overlap
# `fail`, and without the injection below the guard and its `pathsOverlap` helper
# would ship completely unexercised — which is how a forward guard silently rots
# into a comment.
#
# The injection appends one synthetic inventory entry with
# ``relabelsOnMount: true`` to a scratch copy of the chart, standing in for the
# future relabeling volume the guard exists to catch. It is the smallest change
# that reaches the branch: the inventory gains a member, and nothing else moves.

#: Anchor line the synthetic entry is inserted before. Immediately after the
#: inventory is built and before the shared-root partition consumes it.
INVENTORY_ANCHOR = "{{- $sharedRoots := list $cleanDatasetRoot -}}"

#: Owner label the synthetic entry reports. Quote-free, so it needs no escaping
#: when it is written into the Go template dict literal.
SYNTHETIC_RELABELING_OWNER = "future geodata-example-pv local PersistentVolume"


def chart_with_a_relabeling_mount(tmp_path: Path, relabeling_path: str) -> Path:
    """Copy the chart and add one synthetic relabeling mount to the inventory."""
    chart_copy = tmp_path / "node-local-data"
    shutil.copytree(CHART_PATH, chart_copy)

    validate_template = chart_copy / "templates" / "_validate.tpl"
    original = validate_template.read_text(encoding="utf-8")
    assert original.count(INVENTORY_ANCHOR) == 1, (
        "the mount inventory in _validate.tpl no longer has its expected shape; "
        "the forward overlap guard cannot be reached without re-anchoring this "
        "injection"
    )

    injection = (
        "{{- $hostDirectoryMounts = append $hostDirectoryMounts (dict\n"
        f'  "owner" "{SYNTHETIC_RELABELING_OWNER}"\n'
        f'  "path" "{relabeling_path}"\n'
        '  "relabelsOnMount" true\n'
        ") -}}\n"
    )
    validate_template.write_text(
        original.replace(INVENTORY_ANCHOR, injection + INVENTORY_ANCHOR),
        encoding="utf-8",
    )
    return chart_copy


#: Relabeling paths that overlap the default ``/mnt/geodata`` shared root, with
#: the normalized form the failure message must name. Equality, a true child, a
#: containing parent, and two denormalized spellings of the same child.
OVERLAPPING_RELABELING_PATHS = (
    ("/mnt/geodata", "/mnt/geodata"),
    ("/mnt/geodata/geocoding", "/mnt/geodata/geocoding"),
    ("/mnt", "/mnt"),
    ("/mnt/geodata//geocoding", "/mnt/geodata/geocoding"),
    ("/mnt/geodata/routing/../geocoding", "/mnt/geodata/geocoding"),
)


@pytest.mark.parametrize(
    ("relabeling_path", "normalized"), OVERLAPPING_RELABELING_PATHS
)
def test_relabeling_mount_over_the_shared_root_is_rejected_naming_both_paths(
    render_chart, tmp_path: Path, relabeling_path: str, normalized: str
):
    """A relabeling mount overlapping a shared root fails, naming both paths.

    Containment is checked in both directions, so a relabeling volume placed on
    a parent of the routing path is caught as well as one placed on a child, and
    denormalized spellings collapse onto the same directory before comparison.

    The message has to name both paths and explain the asymmetry, because the
    operator's next move is choosing a different path and nothing else in the
    chart tells them what "overlaps" means here.

    **Validates: Requirements 2.1, 3.8**
    """
    chart_copy = chart_with_a_relabeling_mount(tmp_path, relabeling_path)

    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, chart_path=chart_copy)

    message = helm_error(error)
    assert f'relabeling path "{normalized}"' in message
    assert 'shared routing path "/mnt/geodata"' in message
    assert SYNTHETIC_RELABELING_OWNER in message
    assert "MCS categories" in message


def test_relabeling_mount_on_a_non_overlapping_sibling_still_renders(
    render_chart, tmp_path: Path
):
    """The guard must not reject a sibling that merely shares a string prefix.

    ``/mnt/geodata-geocoding`` starts with ``/mnt/geodata`` as a string and is
    not below it as a path. A guard written as a bare prefix test passes the
    rejection cases above and fails here, which is the whole reason this case
    exists.

    **Validates: Requirements 2.1, 3.8**
    """
    chart_copy = chart_with_a_relabeling_mount(tmp_path, "/mnt/geodata-geocoding")

    manifests, notes = render_chart(EXAMPLE_VALUES, chart_path=chart_copy)

    assert manifests
    assert_no_placeholders(manifests, notes)


# --------------------------------------------------------------------------
# datasetRoot containment, including the forms only normalization catches
# --------------------------------------------------------------------------

#: ``/mnt`` is covered by the validation parametrize case above. These are the
#: forms it does not reach: a root outside ``/mnt`` entirely, a sibling mount
#: point sharing the string prefix, and two spellings that only fail once the
#: path is normalized.
NON_MNT_DATASET_ROOTS = (
    "/srv/geodata",
    "/mntother/geodata",
    "/mnt/",
    "/mnt/../srv/geodata",
    "/mnt/geodata/../..",
)


@pytest.mark.parametrize("dataset_root", NON_MNT_DATASET_ROOTS)
def test_dataset_root_outside_mnt_is_rejected_after_normalization(
    render_chart, dataset_root: str
):
    """``datasetRoot`` must be below ``/mnt/`` both as written and once cleaned.

    The raw check alone accepts ``/mnt/../srv/geodata``, which escapes ``/mnt``
    on the node; the cleaned check alone accepts nothing extra but is what
    catches it. Both halves of the condition are load-bearing and each of these
    inputs fails if either half is dropped.

    Bottlerocket only binds instance-store datasets below ``/mnt``, so a root
    that escapes it renders a chart whose bind target does not exist.

    **Validates: Requirements 3.5, 3.8**
    """
    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, set_values=(f"datasetRoot={dataset_root}",))

    assert "datasetRoot must be a non-root path below /mnt/" in helm_error(error)

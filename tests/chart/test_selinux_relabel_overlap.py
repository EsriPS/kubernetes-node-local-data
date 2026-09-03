"""Bug condition exploration test for the geodata SELinux relabel defect.

Bug condition under test, from ``isBugCondition`` in the spec design document:

    A relabeling volume mount covers a path that equals, contains, or is
    contained by a path shared to other consumers through a non-relabeling
    ``hostPath``.

``relabelsOnMount`` is true when the volume plugin reports SELinux support *and*
the backing filesystem carries ``seclabel`` (the ``seclabel`` rule, F2). The
``local`` plugin on Bottlerocket instance-store xfs satisfies both. The in-tree
``hostPath`` plugin fails the first test by design, so it never relabels.

With ``localVolume.enabled=true`` the chart renders a ``local`` PersistentVolume
whose ``spec.local.path`` is ``datasetRoot`` — the same ``datasetRoot`` that the
Path Shim DaemonSet and the sync-agent DaemonSet mount as a ``hostPath`` and that
ArcGIS registers as an organization-wide ``hostPath`` folder data store. One
relabeling mount and several non-relabeling mounts over one directory tree. The
first local-PV pod to start restamps the tree with its own MCS categories and
every other consumer on that node is denied.

SCOPE — READ THIS BEFORE TREATING A GREEN RUN AS PROOF
------------------------------------------------------
A real SELinux relabel CANNOT be reproduced by ``helm template``. Reproducing one
needs an SELinux-enforcing Bottlerocket node, a relabeling ``local`` PV, and a
real pod — and because the label is written on disk per inode (D1) and cannot be
relabeled back on Bottlerocket (F1), triggering one costs a node.

This test proves exactly one thing: that the *configuration expressing the bug*
is representable today, and that after the fix it is rejected. The runtime
behaviour — the relabel itself, the AVC denials, the reference label histogram,
the no-inheritance finding, and the verified node-replacement recovery — is
covered by the recorded live evidence in the spec directory, NOT by this suite.

**A green chart suite is never proof that the relabel is fixed.**

Expected outcome on the unfixed chart: this test FAILS, and its failure message
is the counterexample — ``helm template`` succeeds while rendering the relabeling
PV path and the non-relabeling ``hostPath`` paths as one identical string.

**Validates: Requirements 1.1, 2.1**
"""

import posixpath
import subprocess
from typing import Dict, List

import pytest

from test_render import EXAMPLE_VALUES, helm_error, resource


Resource = Dict[str, object]

# The relabeling volume the chart can create today. Its path is datasetRoot.
RELABELING_RESOURCE = ("PersistentVolume", "geodata-local-pv")

# Non-relabeling hostPath consumers of the same datasetRoot. The ArcGIS routing
# folder data store registers this same path organization-wide; it is not a chart
# resource, so the Path Shim is its in-chart stand-in — the Path Shim exists
# precisely to guarantee that registered path on every schedulable node.
SHARED_HOSTPATH_CONSUMERS = (
    ("DaemonSet", "geodata-path-shim", "dataset-root"),
    ("DaemonSet", "geodata-sync-agent", "node-local-data"),
)

# datasetRoot values parameterised to show the overlap is structural rather than
# an artefact of the default path. Every consumer interpolates datasetRoot
# verbatim, so the relabeling path and the shared hostPath paths coincide for any
# value the chart accepts — including denormalized and nested forms, which a
# lexical `clean` collapses onto the same directory.
OVERLAPPING_DATASET_ROOTS = (
    "/mnt/geodata",
    "/mnt/geodata/",
    "/mnt/alternate-geodata",
    "/mnt/geodata//nested",
    "/mnt/geodata/sibling/../nested",
)


def host_path(daemon_set: Resource, volume_name: str) -> str:
    """Return the hostPath.path of one named volume on a DaemonSet pod spec."""
    volumes = daemon_set["spec"]["template"]["spec"]["volumes"]
    volume = next(item for item in volumes if item["name"] == volume_name)
    return volume["hostPath"]["path"]


def overlapping_paths(manifests: List[Resource]) -> Dict[str, str]:
    """Map each rendered mount path that participates in the overlap."""
    kind, name = RELABELING_RESOURCE
    local_pv = resource(manifests, kind, name)
    paths = {
        f"{kind}/{name} .spec.local.path (RELABELING)": local_pv["spec"]["local"][
            "path"
        ]
    }
    for kind, name, volume_name in SHARED_HOSTPATH_CONSUMERS:
        daemon_set = resource(manifests, kind, name)
        label = f"{kind}/{name} volume {volume_name!r} .hostPath.path"
        paths[f"{label} (NOT relabeled)"] = host_path(daemon_set, volume_name)
    return paths


def counterexample_report(dataset_root: str, paths: Dict[str, str]) -> str:
    """Render the counterexample: a render that succeeded when it should fail."""
    rendered = "\n".join(f"    {label}\n      {path!r}" for label, path in paths.items())
    distinct = set(paths.values())
    return (
        "COUNTEREXAMPLE — the bug condition is representable.\n"
        f"  datasetRoot={dataset_root!r}\n"
        "  helm template SUCCEEDED. It should have been rejected: a relabeling\n"
        "  local PV and non-relabeling hostPath mounts were rendered over one\n"
        "  directory tree.\n"
        f"  Rendered mount paths ({len(distinct)} distinct value(s)):\n"
        f"{rendered}\n"
        "  The first pod to mount the local PV relabels this tree to its own MCS\n"
        "  categories, after which every hostPath consumer on that node is\n"
        "  denied. Mode 0755 root:root does not override an MCS mismatch."
    )


def rejection_explains_the_overlap(message: str, dataset_root: str) -> bool:
    """Accept either fixed-behaviour rejection shape from the design.

    The legacy-key rejection is what makes the overlap unrepresentable today.
    The overlap check is the forward guard for a future relabeling volume, and
    it must name both paths.
    """
    legacy_key_rejection = "localVolume" in message
    normalized = posixpath.normpath(dataset_root)
    overlap_rejection = "overlap" in message.lower() and (
        dataset_root in message or normalized in message
    )
    return legacy_key_rejection or overlap_rejection


@pytest.mark.parametrize("dataset_root", OVERLAPPING_DATASET_ROOTS)
def test_relabeling_local_pv_over_a_shared_hostpath_root_is_rejected(
    render_chart, dataset_root: str
) -> None:
    """A relabeling volume sharing a non-relabeling hostPath tree must not render.

    Assertion, matching the expected-behaviour property: ``renderChart`` FAILS,
    and the failure is either the ``localVolume`` legacy-key rejection or an
    overlap message naming both the relabeling path and the shared root.

    **Validates: Requirements 1.1, 2.1**
    """
    set_values = (
        f"datasetRoot={dataset_root}",
        "localVolume.enabled=true",
    )

    with pytest.raises(subprocess.CalledProcessError) as error:
        manifests, _ = render_chart(EXAMPLE_VALUES, set_values=set_values)
        # Reached only while the bug is present. Fail with the counterexample
        # rather than with a bare "DID NOT RAISE", so the evidence is in the
        # test output.
        paths = overlapping_paths(manifests)
        assert len(set(paths.values())) == 1, counterexample_report(
            dataset_root, paths
        )
        pytest.fail(counterexample_report(dataset_root, paths))

    message = helm_error(error)
    assert rejection_explains_the_overlap(message, dataset_root), message


def test_routing_only_configuration_renders_no_relabeling_volume(
    render_chart,
) -> None:
    """Edge case: with no relabeling volume configured, nothing can relabel.

    This is the baseline the preservation tests lock in, and it passes on the
    unfixed chart. It is the permanent state under the selected fix.
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)

    kinds_and_names = {
        (item["kind"], item["metadata"]["name"]) for item in manifests
    }
    assert RELABELING_RESOURCE not in kinds_and_names
    assert not any(
        "local" in item.get("spec", {}) for item in manifests
    ), "no rendered PersistentVolume may use the relabeling local plugin"

"""Preservation tests for the geodata SELinux relabel fix.

Property 2 (Preservation) from the spec design document: for any configuration
where the bug condition does NOT hold, the fixed chart and agent must produce
the same observable result as the original.

These tests were written and made green against the **unfixed** (post-rollback)
chart, following the observation-first method: the routing-only render, the
registered routing path, the rclone argv, the generation digest, and the
NodePool/EC2NodeClass field set were captured from the unfixed chart first and
recorded, and the assertions below reproduce exactly what was recorded. They are
re-run unchanged after the fix, where a difference outside ``MODULO_DIFFERENCE``
is a regression rather than an intended change.

THE MODULO DIFFERENCE SET IS EXPLICIT AND CLOSED
------------------------------------------------
``MODULO_DIFFERENCE`` below is the literal, closed set of differences the fix is
permitted to introduce. It is enumerated here rather than derived from whatever
the diff happens to show; a preservation check built from the observed diff
degenerates into asserting the new behaviour. Every other rendered field must be
byte-identical.

Two of the four members currently have no rendered occurrence at all, and that
is deliberate:

* ``removedSELinuxSecurityContexts`` was already removed by the rollback, so the
  normalizer that strips ``seLinuxOptions`` is a no-op today. It stays because
  the "no SELinux options anywhere" case below is what locks the rollback in.
* ``chartVersion`` never reaches the rendered manifests — the chart emits no
  ``helm.sh/chart`` label — so stripping it is also a no-op. It stays because
  the version is licensed to move and a future label addition must not be able
  to smuggle a version string into a byte-identical comparison.

WHY ``localVolume.enabled=false`` IS NOT SET EXPLICITLY
------------------------------------------------------
Requirement 3.2 is stated for ``localVolume.enabled`` false. The chart default
is already false, and the fix removes the key entirely, after which setting it
is a hard render failure. Passing it explicitly would make these tests fail for
the intended reason rather than a regression, so the routing-only baseline is
taken at the chart default. ``removedLocalVolumeResources`` is what licenses the
key's disappearance.

For the same reason there is no generated coverage of legacy-key *presence*
here. A values file carrying ``localVolume`` renders today and must fail after
the fix; asserting on it would assert the new behaviour. That case belongs to
the fixed-behaviour suite.

SCOPE — READ THIS BEFORE TREATING A GREEN RUN AS PROOF
------------------------------------------------------
A real SELinux relabel CANNOT be reproduced by ``helm template``. Reproducing
one needs an enforcing Bottlerocket node, a relabeling volume, and a real pod,
and because the label is written on disk per inode (D1) and cannot be relabeled
back on Bottlerocket (F1), triggering one costs a node. This suite proves only
that the configuration expressing the bug is unrepresentable and that everything
outside it is unchanged. The runtime behaviour is covered by the recorded live
evidence in the spec directory.

**A green chart suite is never proof that the relabel is fixed.**

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9**
"""

from pathlib import Path
import os
import posixpath
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Sequence, Tuple

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from test_render import EXAMPLE_VALUES, REPOSITORY_ROOT, resource

sys.path.insert(0, str(REPOSITORY_ROOT / "agent"))
import sync_agent  # noqa: E402

Resource = Dict[str, object]

# --------------------------------------------------------------------------
# The closed MODULO difference set
# --------------------------------------------------------------------------

#: The only differences the fix may introduce. Enumerated as a literal.
MODULO_DIFFERENCE = frozenset(
    {
        "removedLocalVolumeResources",
        "chartVersion",
        "removedSELinuxSecurityContexts",
        "notesText",
    }
)

#: ``removedLocalVolumeResources``: the relabeling volume and its StorageClass.
#: Matched by kind plus the chart-owned name suffix so a non-default
#: ``namePrefix`` is covered too.
REMOVED_LOCAL_VOLUME_RESOURCES = (
    ("StorageClass", "-local-sc"),
    ("PersistentVolume", "-local-pv"),
)

#: ``chartVersion``: label keys that would carry the chart version if the chart
#: ever emitted them. It emits none today, so this is a documented no-op.
CHART_VERSION_KEYS = ("helm.sh/chart",)

#: ``removedSELinuxSecurityContexts``: the rejected shared-MCS-level machinery.
SELINUX_KEYS = ("seLinuxOptions",)

#: ``notesText``: the NOTES body is excluded from the byte-identical render
#: comparison entirely. It is not unchecked — the registered-path case below
#: asserts the only thing about it that is load-bearing, which is that it
#: advertises exactly one dataset path and that path is ``datasetRoot``.

BASELINE_DIR = Path(__file__).resolve().parent / "preservation_baseline"
ROUTING_ONLY_BASELINE = BASELINE_DIR / "routing_only_render.yaml"
NODE_IDENTITY_BASELINE = BASELINE_DIR / "node_identity.yaml"

#: Set to record the observation. Left unset in normal runs, so the recorded
#: baseline is compared against and never silently refreshed by the fix.
RECORD_ENVIRONMENT_VARIABLE = "PRESERVATION_BASELINE_RECORD"

DEFAULT_DATASET_ROOT = "/mnt/geodata"

# --------------------------------------------------------------------------
# Normalization: apply the MODULO set and nothing else
# --------------------------------------------------------------------------


def _strip_keys(node: object, keys: Sequence[str]) -> object:
    """Remove ``keys`` at any depth. Applies one MODULO member, recursively."""
    if isinstance(node, dict):
        return {
            key: _strip_keys(value, keys)
            for key, value in node.items()
            if key not in keys
        }
    if isinstance(node, list):
        return [_strip_keys(item, keys) for item in node]
    return node


def is_removed_local_volume_resource(item: Resource) -> bool:
    """True for a resource the fix is licensed to delete."""
    name = item["metadata"]["name"]
    return any(
        item["kind"] == kind and name.endswith(suffix)
        for kind, suffix in REMOVED_LOCAL_VOLUME_RESOURCES
    )


def resource_identity(item: Resource) -> Tuple[str, str, str]:
    return (
        str(item["kind"]),
        str(item["metadata"].get("namespace", "")),
        str(item["metadata"]["name"]),
    )


def normalize(manifests: Iterable[Resource]) -> str:
    """Serialize a render with the MODULO difference set factored out.

    Everything the MODULO set does not name survives into the returned string,
    so any other change shows up as a byte difference.
    """
    kept = [item for item in manifests if not is_removed_local_volume_resource(item)]
    kept = [
        _strip_keys(_strip_keys(item, SELINUX_KEYS), CHART_VERSION_KEYS)
        for item in kept
    ]
    kept.sort(key=resource_identity)
    return yaml.safe_dump_all(kept, sort_keys=True, default_flow_style=False)


def require_baseline(path: Path) -> str:
    """Read a recorded observation, or say plainly how it was produced."""
    assert path.exists(), (
        f"missing recorded baseline {path.name}; it is observed from the "
        f"unfixed chart by running once with {RECORD_ENVIRONMENT_VARIABLE}=1 "
        "and must not be re-recorded after the fix"
    )
    return path.read_text(encoding="utf-8")


def recorded_baseline(path: Path, observed: str) -> str:
    """Return the recorded observation, writing it once when recording."""
    if os.environ.get(RECORD_ENVIRONMENT_VARIABLE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(observed, encoding="utf-8")
    return require_baseline(path)


# --------------------------------------------------------------------------
# Rendered-surface accessors
# --------------------------------------------------------------------------


def volume_host_path(pod_spec: Dict[str, object], volume_name: str) -> object:
    volume = next(
        item for item in pod_spec["volumes"] if item["name"] == volume_name
    )
    return volume["hostPath"]


def bind_target(node_class: Resource) -> str:
    """The Bottlerocket ephemeral-storage bind target from the bootstrap TOML."""
    match = re.search(
        r'\["apiclient", "ephemeral-storage", "bind", "--dirs", "([^"]+)"\]',
        node_class["spec"]["userData"],
    )
    assert match is not None, "no ephemeral-storage bind command was rendered"
    return match.group(1)


def notes_dataset_paths(notes: str) -> set:
    """Every ``/mnt`` path the NOTES advertise, with trailing punctuation cut.

    Deliberately phrasing-independent. The NOTES body is a MODULO member, so an
    assertion tied to the surrounding wording would break for a licensed reason.
    What must not change is which path an operator is told to register.
    """
    return {
        candidate.rstrip(".,;:)")
        for candidate in re.findall(r"/mnt/[^\s\"']*", notes)
    }


def pod_specs(manifests: Iterable[Resource]) -> List[Tuple[str, Dict[str, object]]]:
    """Every rendered pod spec, labelled by its owner."""
    found = []
    for item in manifests:
        if item["kind"] not in {"DaemonSet", "Deployment", "StatefulSet", "Job"}:
            continue
        owner = f"{item['kind']}/{item['metadata']['name']}"
        found.append((owner, item["spec"]["template"]["spec"]))
    return found


def all_containers(pod_spec: Dict[str, object]) -> List[Dict[str, object]]:
    return [
        container
        for key in ("initContainers", "containers")
        for container in pod_spec.get(key, [])
    ]


# ==========================================================================
# Case 1 — routing-only render preservation (3.2)
# ==========================================================================


def test_routing_only_render_is_identical_modulo_the_difference_set(render_chart):
    """The whole routing-only render is byte-identical modulo the MODULO set.

    Taken at the chart default, where ``localVolume.enabled`` is false, so the
    same invocation is valid before and after the key is removed.

    **Validates: Requirements 3.2, 3.8**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)
    observed = normalize(manifests)

    assert observed == recorded_baseline(ROUTING_ONLY_BASELINE, observed), (
        "the routing-only render changed outside the permitted difference set "
        f"{sorted(MODULO_DIFFERENCE)}"
    )


def test_routing_only_render_contains_no_relabeling_volume(render_chart):
    """The routing-only render carries no volume that can relabel on mount.

    ``relabelsOnMount`` needs a plugin that reports SELinux support; the in-tree
    ``hostPath`` plugin does not. This is the baseline state being preserved.

    **Validates: Requirements 3.2**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)

    for item in manifests:
        if item["kind"] == "PersistentVolume":
            assert "local" not in item["spec"], (
                f"PersistentVolume/{item['metadata']['name']} uses the "
                "relabeling local plugin"
            )


# ==========================================================================
# Case 2 — registered routing path preservation (3.5)
# ==========================================================================


def assert_registered_routing_path(
    manifests: List[Resource], notes: str, dataset_root: str
) -> None:
    """Every routing path surface is exactly ``dataset_root``.

    A change to any one of these forces an ArcGIS re-registration of the folder
    data store, which is the silent failure 3.5 exists to prevent.
    """
    path_shim = resource(manifests, "DaemonSet", "geodata-path-shim")
    sync_agent_daemon_set = resource(manifests, "DaemonSet", "geodata-sync-agent")
    node_class = resource(manifests, "EC2NodeClass", "geodata-nodes")

    path_shim_spec = path_shim["spec"]["template"]["spec"]
    sync_pod = sync_agent_daemon_set["spec"]["template"]["spec"]
    sync_container = sync_pod["containers"][0]

    assert volume_host_path(path_shim_spec, "dataset-root") == {
        "path": dataset_root,
        "type": "DirectoryOrCreate",
    }
    assert volume_host_path(sync_pod, "node-local-data") == {
        "path": dataset_root,
        "type": "DirectoryOrCreate",
    }
    assert [
        argument
        for argument in sync_container["args"]
        if argument.startswith("--dest-root=")
    ] == [f"--dest-root={dataset_root}"]
    assert bind_target(node_class) == dataset_root
    assert notes_dataset_paths(notes) == {dataset_root}


def test_registered_routing_path_defaults_to_the_documented_mount(render_chart):
    """The registered path is exactly ``/mnt/geodata`` on the documented values.

    **Validates: Requirements 3.5**
    """
    manifests, notes = render_chart(EXAMPLE_VALUES)

    assert_registered_routing_path(manifests, notes, DEFAULT_DATASET_ROOT)


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    segment=st.text(
        # ASCII only. Bottlerocket dataset paths are ASCII, and a non-ASCII path
        # is escaped when Helm renders it into the bootstrap TOML, which is a
        # separate rendering concern and not part of the preserved domain.
        alphabet=st.sampled_from(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        ),
        min_size=1,
        max_size=24,
    ).filter(lambda value: value.strip("-_") != "")
)
def test_registered_routing_path_follows_dataset_root_everywhere(
    render_chart, segment: str
):
    """Property: every path surface tracks ``datasetRoot`` verbatim.

    Generated over the non-bug domain — normalized single-segment roots below
    ``/mnt/``. Denormalized and nested forms belong to the overlap tests, not
    here. ``render_chart`` is a stateless factory fixture, so reusing it across
    generated examples is safe.

    **Validates: Requirements 3.5**
    """
    dataset_root = f"/mnt/{segment}"
    manifests, notes = render_chart(
        EXAMPLE_VALUES, set_values=(f"datasetRoot={dataset_root}",)
    )

    assert_registered_routing_path(manifests, notes, dataset_root)


# ==========================================================================
# Case 3 — sync semantics preservation (3.3)
# ==========================================================================

#: Observed on the unfixed agent: the complete rclone argv shape, in order.
RECORDED_RCLONE_FLAGS = (
    "--metadata",
    "--transfers=8",
    "--multi-thread-streams=8",
    "--multi-thread-cutoff=256M",
    "--multi-thread-chunk-size=64M",
    "--multi-thread-write-buffer-size=4M",
    "--checkers=16",
    "--stats=60s",
    "--stats-one-line",
    "-v",
)

RECORDED_CHART_EXCLUDES = ("*.zip.*", "*.zip")


@pytest.fixture
def recorded_rclone_invocations(monkeypatch):
    """Capture argv from ``sync_pass`` without running rclone."""
    invocations: List[List[str]] = []

    def fake_run(
        cmd: List[str], check: bool = True, *, stream_stderr: bool = False
    ) -> subprocess.CompletedProcess:
        invocations.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr="300 KiB / 300 KiB, 100%, 0 B/s, ETA -\n"
        )

    monkeypatch.setattr(sync_agent, "run", fake_run)
    return invocations


def test_sync_pass_invocation_shape_is_unchanged(
    recorded_rclone_invocations, tmp_path
):
    """One recursive ``rclone sync``, ``--metadata`` present, no ``--inplace``.

    Locks the direct-mirror semantics: per-file rename replacement, ownership
    and mode preserved, excludes honoured, measured concurrency. If the optional
    stderr-streaming hardening is applied it must not change this argv.

    **Validates: Requirements 3.3**
    """
    source = tmp_path / "source"
    destination = tmp_path / "geodata"
    source.mkdir()

    ok, completed = sync_agent.sync_pass(
        source,
        destination,
        excludes=list(RECORDED_CHART_EXCLUDES),
        transfers=8,
        streams=8,
    )

    assert (ok, completed) == (True, 1)
    assert len(recorded_rclone_invocations) == 1, (
        "the pass must run exactly one recursive rclone sync"
    )
    argv = recorded_rclone_invocations[0]

    assert argv[:2] == ["rclone", "sync"]
    assert argv[2] == f"{source}{os.sep}"
    assert argv[3] == f"{destination}{os.sep}"
    assert "--inplace" not in argv
    for flag in RECORDED_RCLONE_FLAGS:
        assert flag in argv, f"{flag} disappeared from the rclone invocation"

    exclude_pairs = tuple(
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "--exclude"
    )
    assert exclude_pairs == RECORDED_CHART_EXCLUDES


def test_chart_passes_the_recorded_sync_arguments(render_chart):
    """The DaemonSet still hands the agent the recorded flags and excludes.

    **Validates: Requirements 3.3**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)
    sync_container = resource(manifests, "DaemonSet", "geodata-sync-agent")["spec"][
        "template"
    ]["spec"]["containers"][0]
    args = sync_container["args"]

    assert args[0] == "--source=/mnt/source"
    assert "--transfers=8" in args
    assert "--multi-thread-streams=8" in args
    assert "--interval=300" in args
    assert [
        argument for argument in args if argument.startswith("--exclude=")
    ] == [f"--exclude={pattern}" for pattern in RECORDED_CHART_EXCLUDES]
    assert not any("inplace" in argument for argument in args)


# ==========================================================================
# Case 4 — readiness reconciliation preservation (3.3)
# ==========================================================================

#: Observed on the unfixed agent. Digests are truncated SHA-256 over the sorted
#: names joined by newlines, so they are recorded literals rather than a
#: re-implementation of the function under test.
RECORDED_GENERATIONS = (
    ([], "none", "0"),
    (["GEODATA2026Q2"], "gda38dcffebc1", "1"),
    (["GEODATA2026Q1", "GEODATA2026Q2", "GEODATA2026Q3"], "g4d7d3b6aedbf", "3"),
    (["Locators_VENDOR", "Network_VENDOR", "Routing_VENDOR"], "g7d247bb41d03", "3"),
)


@pytest.mark.parametrize(("datasets", "generation", "count"), RECORDED_GENERATIONS)
def test_generation_digest_and_count_match_the_recorded_values(
    datasets: List[str], generation: str, count: str
):
    """The readiness identity for a fixed name set is unchanged.

    Nodes that already carry these label values must not be relabelled by the
    fix, because the labels are what publish-time checks read.

    **Validates: Requirements 3.3, 3.9**
    """
    assert sync_agent.generation_value(datasets) == generation
    assert str(len(datasets)) == count


@settings(max_examples=200, deadline=None)
@given(
    datasets=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
            min_size=1,
            max_size=16,
        ),
        min_size=0,
        max_size=12,
        unique=True,
    ),
    rotation=st.integers(min_value=0, max_value=11),
)
def test_generation_digest_is_order_independent_and_within_the_label_limit(
    datasets: List[str], rotation: int
):
    """Property: the digest depends on the name set, not the enumeration order.

    ``list_datasets`` already sorts, but the digest must not become
    order-sensitive, or a node whose directory listing order changes would be
    relabelled for no reason. Node label values cap at 63 characters.

    **Validates: Requirements 3.3**
    """
    if datasets:
        offset = rotation % len(datasets)
        rotated = datasets[offset:] + datasets[:offset]
    else:
        rotated = []

    value = sync_agent.generation_value(datasets)

    assert value == sync_agent.generation_value(rotated)
    assert value == sync_agent.generation_value(list(reversed(datasets)))
    assert len(value) <= 63
    assert (value == "none") == (datasets == [])


def test_write_generation_label_still_suppresses_identical_writes(monkeypatch):
    """Read-before-write is intact, so the per-interval write storm stays fixed.

    An unconditional write produced one API write, one audit event, and one
    watch notification per node per interval, forever.

    **Validates: Requirements 3.3**
    """
    datasets = ["GEODATA2026Q1", "GEODATA2026Q2", "GEODATA2026Q3"]
    generation = sync_agent.generation_value(datasets)
    writes: List[List[str]] = []

    def fake_run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
        writes.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sync_agent, "run", fake_run)
    monkeypatch.setattr(
        sync_agent,
        "read_node_labels",
        lambda node: {
            sync_agent.GENERATION_LABEL: generation,
            sync_agent.DATASET_COUNT_LABEL: str(len(datasets)),
        },
    )

    assert sync_agent.write_generation_label("node-a", datasets) is True
    assert writes == [], "an already-correct node must not be relabelled"

    monkeypatch.setattr(sync_agent, "read_node_labels", lambda node: {})
    assert sync_agent.write_generation_label("node-a", datasets) is True
    assert len(writes) == 1
    assert writes[0][:3] == ["kubectl", "label", "node"]
    assert f"{sync_agent.GENERATION_LABEL}={generation}" in writes[0]
    assert f"{sync_agent.DATASET_COUNT_LABEL}={len(datasets)}" in writes[0]


def test_write_generation_label_rewrites_when_labels_are_unreadable(monkeypatch):
    """Unknown labels mean write, not assume. Self-healing behaviour preserved.

    **Validates: Requirements 3.3**
    """
    writes: List[List[str]] = []

    def fake_run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
        writes.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sync_agent, "run", fake_run)
    monkeypatch.setattr(sync_agent, "read_node_labels", lambda node: None)

    assert sync_agent.write_generation_label("node-a", ["GEODATA2026Q2"]) is True
    assert len(writes) == 1


# ==========================================================================
# Case 5 — NodePool and EC2NodeClass identity preservation (3.7, 3.9)
# ==========================================================================


def node_identity_render(manifests: List[Resource]) -> str:
    """Serialize the two resources whose drift costs every node its mirror."""
    return normalize(
        [
            resource(manifests, "EC2NodeClass", "geodata-nodes"),
            resource(manifests, "NodePool", "geodata-nodes"),
        ]
    )


def test_node_pool_and_node_class_are_byte_identical(render_chart):
    """Every NodePool and EC2NodeClass field is byte-identical.

    Any change here can mark NodeClaims Drifted, and replacement discards a
    node's multi-hundred-GB mirror. This is the assertion that keeps the fix
    from costing data as a side effect.

    **Validates: Requirements 3.7, 3.9**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)
    observed = node_identity_render(manifests)

    assert observed == recorded_baseline(NODE_IDENTITY_BASELINE, observed), (
        "NodePool or EC2NodeClass drifted; NodeClaim replacement discards each "
        "node's local mirror"
    )


#: Values that have nothing to do with node identity. Changing any of them must
#: leave the NodePool and the EC2NodeClass untouched.
UNRELATED_VALUE_OVERRIDES = st.sampled_from(
    [
        ("syncAgent.intervalSeconds=60",),
        ("syncAgent.memory=2Gi",),
        ("syncAgent.terminationGracePeriodSeconds=600",),
        ("pathShim.enabled=false",),
        ("warmCapacity.replicas=1",),
        ("nfs.capacity=2048Gi",),
        ("image.pullPolicy=Always",),
    ]
)


@settings(
    max_examples=7,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(set_values=UNRELATED_VALUE_OVERRIDES)
def test_unrelated_values_never_disturb_node_identity(
    render_chart, set_values: Tuple[str, ...]
):
    """Property: no unrelated value can drift the node-identity resources.

    **Validates: Requirements 3.7, 3.9**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES, set_values=set_values)
    observed = node_identity_render(manifests)

    assert observed == require_baseline(NODE_IDENTITY_BASELINE), (
        f"{set_values} changed the NodePool or EC2NodeClass"
    )


def test_disruption_settings_and_ami_alias_are_pinned(render_chart):
    """The specific fields whose change forces replacement, spelled out.

    Kept alongside the byte-identical comparison as a readable anchor: a
    reviewer should not have to open a baseline file to see what matters.

    **Validates: Requirements 3.7, 3.9**
    """
    manifests, _ = render_chart(EXAMPLE_VALUES)
    node_class = resource(manifests, "EC2NodeClass", "geodata-nodes")
    node_pool = resource(manifests, "NodePool", "geodata-nodes")
    template_spec = node_pool["spec"]["template"]["spec"]

    assert node_class["spec"]["amiSelectorTerms"] == [
        {"alias": "bottlerocket@v1.32.0"}
    ]
    assert node_pool["spec"]["disruption"] == {
        "consolidationPolicy": "WhenEmpty",
        "consolidateAfter": "Never",
        "budgets": [{"nodes": "0"}],
    }
    assert template_spec["expireAfter"] == "Never"
    assert [taint["key"] for taint in template_spec["taints"]] == [
        "data.example.com/dedicated"
    ]
    assert [taint["key"] for taint in template_spec["startupTaints"]] == [
        "data.example.com/data-not-ready"
    ]
    assert template_spec["nodeClassRef"]["name"] == "geodata-nodes"


# ==========================================================================
# Case 6 — no SELinux options anywhere (3.6, and the task 1 rollback)
# ==========================================================================

SELINUX_VARIANT_VALUES = (
    (),
    ("pathShim.enabled=false",),
    ("warmCapacity.enabled=false",),
    ("localVolume.enabled=true",),
)


@pytest.mark.parametrize("set_values", SELINUX_VARIANT_VALUES)
def test_no_rendered_pod_or_container_carries_selinux_options(
    render_chart, set_values: Tuple[str, ...]
):
    """No ``seLinuxOptions`` anywhere, and no container privileged.

    Locks in the rollback of the rejected shared-MCS-level approach. A pinned
    shared level had no reachable success state: MCS categories are assigned per
    pod sandbox, and two pods of one ReplicaSet already hold different pairs.

    The ``localVolume.enabled=true`` variant is included because the rollback
    must hold on every values shape the chart accepts today. It renders now and
    is rejected after the fix; the SELinux assertion is asserted only on the
    renders that succeed, so the case survives either way.

    **Validates: Requirements 3.4, 3.6**
    """
    try:
        manifests, notes = render_chart(EXAMPLE_VALUES, set_values=set_values)
    except subprocess.CalledProcessError:
        pytest.skip(f"{set_values} is no longer representable; nothing to render")

    serialized = yaml.safe_dump_all(manifests, sort_keys=True) + notes
    assert "seLinuxOptions" not in serialized
    assert "sharedLevel" not in serialized

    for owner, pod_spec in pod_specs(manifests):
        assert "seLinuxOptions" not in pod_spec.get("securityContext", {}), owner
        for container in all_containers(pod_spec):
            security_context = container.get("securityContext", {})
            assert "seLinuxOptions" not in security_context, owner
            assert security_context.get("privileged", False) is False, (
                f"{owner} container {container['name']} became privileged"
            )


def test_chart_surfaces_carry_no_shared_selinux_level_setting(render_chart):
    """The rejected ``selinux.sharedLevel`` value is gone from the surface.

    **Validates: Requirements 3.6**
    """
    # No comma in the value: Helm reads a comma as a --set list separator, so
    # `s0:c1,c2` fails on argument parsing before the schema is consulted and
    # would pass this test for the wrong reason.
    with pytest.raises(subprocess.CalledProcessError) as error:
        render_chart(EXAMPLE_VALUES, set_values=("selinux.sharedLevel=s0",))

    message = f"{error.value.stdout or ''}\n{error.value.stderr or ''}"
    assert "selinux" in message
    assert "additionalProperties" in message


# ==========================================================================
# Case 7 — non-overlapping siblings still render
# ==========================================================================

#: The pair a naive prefix comparison gets wrong. ``/mnt/geodata-geocoding``
#: starts with ``/mnt/geodata`` as a string but is not below it as a path.
SIBLING_DATASET_ROOTS = ("/mnt/geodata", "/mnt/geodata-geocoding")


def lexically_overlapping(first: str, second: str) -> bool:
    """The design's containment rule: normalize, then test both directions.

    Containment is tested with an explicit trailing separator on the prefix, so
    a true child matches and a shared-prefix sibling does not.
    """
    left = posixpath.normpath(first)
    right = posixpath.normpath(second)
    if left == right:
        return True
    return right.startswith(f"{left}/") or left.startswith(f"{right}/")


def test_shared_prefix_siblings_are_not_overlapping_paths():
    """``/mnt/geodata`` and ``/mnt/geodata-geocoding`` do not overlap.

    Stated as an executable expectation of the rule the overlap guard must
    implement, so the guard cannot be written as a bare string-prefix test and
    reject the very configuration the fix exists to permit. A true child, by
    contrast, does overlap.

    **Validates: Requirements 3.2**
    """
    first, second = SIBLING_DATASET_ROOTS

    assert not lexically_overlapping(first, second)
    assert not lexically_overlapping(second, first)
    assert lexically_overlapping(first, "/mnt/geodata/geocoding")
    assert lexically_overlapping("/mnt/geodata/geocoding", first)
    assert lexically_overlapping(first, "/mnt/geodata/")


@pytest.mark.parametrize("dataset_root", SIBLING_DATASET_ROOTS)
def test_shared_prefix_sibling_roots_both_render(render_chart, dataset_root: str):
    """Both sibling roots render, and each renders its own path everywhere.

    This is the guard against an over-aggressive overlap check. Rendering must
    stay possible for a root that shares a string prefix with another managed
    root.

    **Validates: Requirements 3.2, 3.5, 3.8**
    """
    manifests, notes = render_chart(
        EXAMPLE_VALUES, set_values=(f"datasetRoot={dataset_root}",)
    )

    assert_registered_routing_path(manifests, notes, dataset_root)


def test_both_example_values_files_still_render(render_chart):
    """Requirement 3.8's rendering gate, as a test rather than a shell step.

    **Validates: Requirements 3.8**
    """
    for values_file in (
        EXAMPLE_VALUES,
        REPOSITORY_ROOT / "examples" / "values-instance-types.yaml",
    ):
        manifests, notes = render_chart(values_file)
        assert manifests
        assert notes

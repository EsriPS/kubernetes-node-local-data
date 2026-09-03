"""Shared fixtures for focused Helm chart render tests."""

import itertools
from pathlib import Path
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHART_PATH = REPOSITORY_ROOT / "charts" / "node-local-data"
VALIDATE_TEMPLATE = CHART_PATH / "templates" / "_validate.tpl"
RenderResult = Tuple[List[object], str]
PathPair = Tuple[str, str]


@pytest.fixture
def render_chart() -> Callable[..., RenderResult]:
    """Render one chart case once and return its manifests and NOTES."""

    def render(
        values_files: Union[Path, Sequence[Path]] = (),
        *,
        set_values: Sequence[str] = (),
        release_name: str = "node-local-data-test",
        namespace: str = "node-local-data-test",
        chart_path: Path = CHART_PATH,
    ) -> RenderResult:
        """Render a chart case.

        ``chart_path`` defaults to the repository chart and every existing case
        leaves it alone. It is overridable so a test can render a scratch copy of
        the chart, which is the only way to exercise the forward overlap guard in
        ``_validate.tpl``: the guard's relabeling-mount set is empty by
        construction today, so no values file can reach it.
        """
        if isinstance(values_files, Path):
            values_files = (values_files,)

        command = [
            "helm",
            "install",
            release_name,
            str(chart_path),
            "--dry-run",
            "--debug",
            "--namespace",
            namespace,
        ]
        for values_file in values_files:
            command.extend(("--values", str(values_file)))
        for value in set_values:
            command.extend(("--set", value))

        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        manifest_header = "MANIFEST:\n"
        if manifest_header not in completed.stdout:
            raise AssertionError("Helm output did not contain a MANIFEST section")

        rendered = completed.stdout.split(manifest_header, 1)[1]
        manifest_stream, notes_header, notes = rendered.partition("\nNOTES:\n")
        manifests = [
            document
            for document in yaml.safe_load_all(manifest_stream)
            if document is not None
        ]

        return manifests, notes.strip() if notes_header else ""

    return render


# ==========================================================================
# Direct invocation of node-local-data.pathsOverlap
# ==========================================================================
#
# ``pathsOverlap`` is a bare Go-template ``define``. Nothing outside a rendered
# template can call it, and inside the real chart the only call site is the
# forward overlap guard whose relabeling-mount set is empty by construction. So
# reaching the helper through the real chart means reaching it through the
# guard, which is what ``test_render.py`` does by injecting a synthetic
# ``relabelsOnMount: true`` inventory entry into a scratch chart copy. That is
# the right harness for the guard — it renders one path pair per helm invocation
# and asserts a render outcome — but it is the wrong harness for the helper's
# normalization, which needs many pairs and needs to read the helper's own
# boolean rather than a pass/fail render.
#
# So the probe below is the second half of the same idea rather than a second
# idea: still a scratch chart in a temporary directory, still ``render_chart``
# with its ``chart_path`` override, but the scratch chart is minimal — a
# ``Chart.yaml``, a verbatim copy of the real ``_validate.tpl``, and one probe
# template that calls the helper the way the guard calls it and writes the
# result into a ConfigMap. The helper under test is therefore the real one, byte
# for byte, while the surrounding chart contributes nothing.
#
# The probe batches every pair into one ConfigMap, so a table of cases costs one
# helm invocation rather than one per case, and it memoizes results per pair so a
# property-based caller re-drawing the same pair pays nothing.

PROBE_CHART_YAML = """\
apiVersion: v2
name: paths-overlap-probe
description: >-
  Scratch chart that exists only to invoke node-local-data.pathsOverlap
  directly. Never installed; rendered by the chart test suite.
type: application
version: 0.0.0
"""

#: Calls the helper exactly as the forward guard in ``_validate.tpl`` does —
#: ``include`` with a two-element list — and quotes the result so the empty
#: string survives YAML parsing as ``""`` rather than as null.
PROBE_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: paths-overlap-probe
data:
{{- range $index, $pair := .Values.pairs }}
  {{ printf "pair-%04d" $index | quote }}: {{ include "node-local-data.pathsOverlap" (list (index $pair 0) (index $pair 1)) | quote }}
{{- end }}
"""

#: What the helper emits on overlap. A template define can only return text, so
#: every call site compares against this literal.
OVERLAP_TRUE = "true"


def build_paths_overlap_probe_chart(
    destination: Path, *, validate_template: Optional[str] = None
) -> Path:
    """Materialize the minimal probe chart and return its directory.

    ``validate_template`` defaults to the real ``_validate.tpl``, copied
    verbatim, so the helper exercised is the shipped one. It is overridable so a
    mutation check can build the same probe over a deliberately broken copy
    without ever touching the chart in the repository.
    """
    if validate_template is None:
        validate_template = VALIDATE_TEMPLATE.read_text(encoding="utf-8")

    chart = destination / "paths-overlap-probe"
    templates = chart / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (chart / "Chart.yaml").write_text(PROBE_CHART_YAML, encoding="utf-8")
    (templates / "_validate.tpl").write_text(validate_template, encoding="utf-8")
    (templates / "probe.yaml").write_text(PROBE_TEMPLATE, encoding="utf-8")
    return chart


class PathsOverlapProbe:
    """Evaluate ``node-local-data.pathsOverlap`` over concrete path pairs.

    Call it with two paths for a single answer, or use :meth:`batch` for a table
    of pairs in one helm invocation. Results are memoized per pair.
    """

    def __init__(
        self,
        render: Callable[..., RenderResult],
        chart_path: Path,
        values_directory: Path,
    ) -> None:
        self._render = render
        self._chart_path = chart_path
        self._values_directory = values_directory
        self._results: Dict[PathPair, bool] = {}
        self._counter = itertools.count()

    def __call__(self, path_a: str, path_b: str) -> bool:
        return self.batch(((path_a, path_b),))[0]

    def batch(self, pairs: Sequence[PathPair]) -> List[bool]:
        """Return one overlap verdict per pair, preserving input order."""
        pairs = [(str(path_a), str(path_b)) for path_a, path_b in pairs]
        pending = [pair for pair in dict.fromkeys(pairs) if pair not in self._results]
        if pending:
            self._results.update(zip(pending, self._evaluate(pending)))
        return [self._results[pair] for pair in pairs]

    def _evaluate(self, pairs: Sequence[PathPair]) -> List[bool]:
        values_file = (
            self._values_directory / f"probe-{next(self._counter):04d}.yaml"
        )
        values_file.write_text(
            yaml.safe_dump({"pairs": [list(pair) for pair in pairs]}),
            encoding="utf-8",
        )

        manifests, _ = self._render(values_file, chart_path=self._chart_path)
        configmap = next(
            item for item in manifests if item["kind"] == "ConfigMap"
        )
        data = configmap.get("data") or {}
        assert len(data) == len(pairs), (
            f"probe returned {len(data)} result(s) for {len(pairs)} pair(s); the "
            "probe template no longer emits one entry per pair"
        )

        verdicts = []
        for index, pair in enumerate(pairs):
            emitted = data[f"pair-{index:04d}"]
            assert emitted in (OVERLAP_TRUE, ""), (
                f"pathsOverlap{pair} emitted {emitted!r}; the helper must emit "
                f"{OVERLAP_TRUE!r} on overlap and the empty string otherwise"
            )
            verdicts.append(emitted == OVERLAP_TRUE)
        return verdicts


@pytest.fixture(scope="session")
def paths_overlap_probe_chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the probe chart once per session; it is read-only afterwards."""
    return build_paths_overlap_probe_chart(
        tmp_path_factory.mktemp("paths-overlap-probe-chart")
    )


@pytest.fixture
def paths_overlap(
    render_chart: Callable[..., RenderResult],
    paths_overlap_probe_chart: Path,
    tmp_path: Path,
) -> PathsOverlapProbe:
    """Evaluate ``node-local-data.pathsOverlap`` directly.

    ``paths_overlap("/mnt/geodata", "/mnt/geodata/geocoding")`` is True;
    ``paths_overlap.batch(pairs)`` answers a whole table in one helm call.
    """
    return PathsOverlapProbe(render_chart, paths_overlap_probe_chart, tmp_path)

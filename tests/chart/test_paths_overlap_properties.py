"""Property-based cases for the ``node-local-data.pathsOverlap`` helper.

``test_paths_overlap.py`` pins the helper against a fixed table of hand-chosen
pairs. This module states the same contract as properties over generated paths,
because the failures the helper can still have are normalization failures, and
normalization failures are exactly the kind that survive a finite table: a
hand-written case list can only contain the denormalized forms someone thought
of. Here the denormalized forms are generated — duplicate separators, ``.``
segments, ``x/..`` round trips, trailing separators, and every combination of
them — and the property is that none of them may change a verdict.

The properties, and what each one is load-bearing for:

* **P1 reflexivity** — a path always overlaps itself. Pins ``eq``.
* **P2 root** — ``/`` overlaps every absolute path, in both argument orders.
  Pins the ``trimSuffix "/"`` that turns ``clean``'s ``"/"`` into ``""``;
  without it the root's ``"%s/"`` prefix is ``"//"`` and matches nothing.
* **P3 ancestor and descendant** — a path overlaps every path below it, at any
  depth, in both argument orders. Pins both containment directions.
* **P4 segment boundary** — a path does NOT overlap a path formed by appending
  to its final segment. This is the property a naive ``hasPrefix`` violates, and
  the reason the helper appends a separator before comparing.
* **P5 distinct siblings** — two subtrees that diverge at some segment never
  overlap, whatever hangs below them.
* **P6 symmetry** — swapping the arguments never changes the verdict.
* **P7 normalization invariance** — denormalizing either operand, or both, never
  changes the verdict. The strongest property here, and the one aimed squarely at
  the ``clean`` step.

How this differs from the symmetry assertion already in ``test_paths_overlap.py``
(P6): that one sweeps the fixed table, so it proves symmetry only over the pairs
someone wrote down, all of which are one or two segments deep and none of which
are denormalized on both sides at once. P6 here draws from every construction in
this module — including the denormalized ones — and in particular exercises pairs
where both operands are denormalized differently, which the table never does.

Cost. Every property draws a **list** of pairs per example and answers the whole
list with one ``paths_overlap.batch`` call, so an example costs one helm
invocation rather than one per pair, and the probe's per-pair memo makes a
re-drawn pair free. That is what makes generation affordable at all: the helper
lives inside a Go template and the only way to evaluate it is to render a chart.
``max_examples`` is set per property below and is deliberately modest for the
same reason; the breadth comes from the pairs inside each example.

LEXICAL ONLY — DO NOT OVER-TRUST A GREEN RUN
--------------------------------------------
Every property here is about path *strings*. ``helm template`` runs nowhere near
a node and cannot resolve symlinks, bind mounts, or hard links, so two distinct
paths that alias the same inodes satisfy all seven properties and still break at
runtime. SELinux labels live on inodes, not on paths: distinct paths are
necessary, not sufficient, and the operator must also ensure distinct inodes. See
``tuning/gotchas.md``.

The standing scope note applies here as to every chart test touching this defect:
a real SELinux relabel cannot be reproduced by ``helm template``. These cases
prove that the configuration expressing the bug is unrepresentable, and nothing
more. The runtime behaviour is covered by the recorded live evidence in the spec
directory. **A green chart suite is never proof that the relabel is fixed.**

**Validates: Requirements 2.1, 3.8**
"""

import posixpath
from typing import Callable, Dict, List, Sequence, Tuple

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# ==========================================================================
# Mutation matrix
# ==========================================================================
#
# Falsifiability was established by mutation, over scratch copies of
# _validate.tpl built through `build_paths_overlap_probe_chart`'s
# `validate_template` override in a temporary directory — never the file in the
# chart. Rows are mutants of the helper; columns are the properties above. `x`
# means that property failed on that mutant, which is what makes it a test rather
# than a decoration.
#
#                                    P1  P2  P3  P4  P5  P6  P7
#   drop `clean` both operands        .   .   .   .   .   .   x
#   drop `trimSuffix "/"`             .   x   .   .   .   .   .
#   drop the prefix separator         .   .   .   x   .   .   .
#   drop the a-contains-b direction   .   x   x   .   .   x   .
#   drop the b-contains-a direction   .   x   x   .   .   x   .
#   drop `eq`                         x   .   .   .   .   .   .
#   always true                       .   .   .   x   x   .   .
#   always false                      x   x   x   .   .   .   .
#
# Every mutant is caught by at least one property and every property catches at
# least one mutant. Two results are worth recording because they are not what you
# would guess:
#
# * Dropping `clean` is caught ONLY by P7. Every other property builds its paths
#   in normalized form, so an unnormalized helper answers them all correctly.
#   That is the whole reason P7 exists, and the reason this module exists rather
#   than a few more rows in the fixed table.
# * Dropping either containment direction is caught by P2 as well as P3, because
#   the root case is itself a containment case: `/` contains a path without
#   equalling it. P2 and P3 fail together on those two mutants.
#
# `always false` is not a serious implementation error, but it is the degenerate
# mutant the positive properties exist to exclude, so it is recorded for symmetry
# with `always true`.


# ==========================================================================
# Strategies
# ==========================================================================
#
# Segments are constrained to what the guard can actually be handed. Both call
# sites in `_validate.tpl` compare paths derived from `datasetRoot`, which the
# same file already requires to be below `/mnt/`, so every generated path is
# `/mnt/` plus one to three further components.
#
# The alphabet contains NO dot, so a generated segment can never be `.`, `..`, or
# empty. That exclusion is deliberate rather than incidental:
#
# * A bare `.` or `..` segment does not name the directory the surrounding
#   construction says it names — `/mnt/a/..` is `/mnt`, not a child of `/mnt/a` —
#   so admitting one would make P3's "descendant" and P5's "siblings" claims
#   false about their own inputs rather than false about the helper.
# * An empty segment is a duplicate separator, and duplicate separators are a
#   normalization input, generated on purpose by the denormalizers in P7 where
#   they belong.
# * Neither is reachable from the chart anyway: `clean` has already run on
#   `datasetRoot` before the guard sees it, and 5.2 recorded that `clean("")` is
#   `"."` and that degenerate relative inputs cannot arrive from any values file.
#
# `..` and `.` therefore appear in this module only inside P7's denormalizers and
# in P4's appended suffix, in both cases as forms whose meaning is known.

SEGMENT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

#: One realistic path component.
SEGMENTS = st.text(alphabet=SEGMENT_ALPHABET, min_size=1, max_size=12)

#: A dataset-root-shaped absolute path: `/mnt/` and one to three components.
DATASET_PATHS = st.lists(SEGMENTS, min_size=1, max_size=3).map(
    lambda tail: "/".join(["/mnt", *tail])
)

#: A non-empty run of components to hang below a path.
DESCENDANT_TAILS = st.lists(SEGMENTS, min_size=1, max_size=3)

#: Appended to a path's final segment. Dots are allowed here — `/mnt/geodata.old`
#: and `/mnt/geodata..` are realistic siblings — and cannot produce a bare `.` or
#: `..` segment, because the segment they extend already starts with a non-dot.
SEGMENT_SUFFIXES = st.text(alphabet=SEGMENT_ALPHABET + ".", min_size=1, max_size=8)


def _components(path: str) -> List[str]:
    return [component for component in path.split("/") if component]


# --------------------------------------------------------------------------
# Denormalizers
# --------------------------------------------------------------------------
#
# Each rewrites a path into a different spelling of the same directory. They are
# named so a counterexample prints as a recipe rather than as an opaque string,
# and every property that uses them re-proves that each one is meaning-preserving
# with `posixpath.normpath` before trusting it (see `_assert_spelling_only`), so a
# broken denormalizer reports itself instead of reporting a false counterexample.
#
# The leading separator is never doubled. Go's `path.Clean` collapses `//x` to
# `/x` while POSIX and `posixpath.normpath` keep a leading `//` as
# implementation-defined, and that divergence would break the oracle above
# without saying anything about the helper.

ROUND_TRIP_COMPONENT = "roundtrip"


def _rebuild(path: str, separator: str) -> str:
    components = _components(path)
    return "/" + separator.join(components)


def _identity(path: str) -> str:
    return path


def _duplicate_separators(path: str) -> str:
    return _rebuild(path, "//")


def _trailing_separator(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def _dot_components(path: str) -> str:
    components = _components(path)
    if not components:
        return "/."
    interleaved: List[str] = []
    for component in components:
        interleaved.extend((".", component))
    return "/" + "/".join(interleaved)


def _dot_dot_round_trips(path: str) -> str:
    components = _components(path)
    if not components:
        return f"/{ROUND_TRIP_COMPONENT}/.."
    expanded: List[str] = []
    for component in components:
        expanded.extend((ROUND_TRIP_COMPONENT, "..", component))
    return "/" + "/".join(expanded)


def _every_denormalization(path: str) -> str:
    return _trailing_separator(
        _duplicate_separators(_dot_components(_dot_dot_round_trips(path)))
    )


DENORMALIZERS: Dict[str, Callable[[str], str]] = {
    "identity": _identity,
    "duplicate-separators": _duplicate_separators,
    "trailing-separator": _trailing_separator,
    "dot-components": _dot_components,
    "dot-dot-round-trips": _dot_dot_round_trips,
    "every-denormalization": _every_denormalization,
}

DENORMALIZER_NAMES = st.sampled_from(sorted(DENORMALIZERS))


def _assert_spelling_only(original: str, rewritten: str, recipe: str) -> None:
    """Fail the *test harness* if a denormalizer changed which directory is named.

    Uses ``posixpath.normpath`` rather than the helper, so this is an independent
    check on the generated input and not a re-implementation of the code under
    test.
    """
    assert posixpath.normpath(rewritten) == posixpath.normpath(original), (
        f"denormalizer {recipe!r} rewrote {original!r} as {rewritten!r}, which "
        "names a different directory; the harness is wrong, not the helper"
    )


# --------------------------------------------------------------------------
# Pair constructions
# --------------------------------------------------------------------------
#
# Every construction yields (path_a, path_b, expected_overlap, construction) so a
# counterexample says which construction produced it. Each draws its own
# orientation, so both argument orders are generated rather than assumed
# equivalent — P6 is what establishes the equivalence, and it must not be
# presupposed by the strategies feeding it.

PairCase = Tuple[str, str, bool, str]


@st.composite
def equal_pairs(draw: st.DrawFn) -> PairCase:
    path = draw(DATASET_PATHS)
    return (path, path, True, "equal")


@st.composite
def descendant_pairs(draw: st.DrawFn) -> PairCase:
    ancestor = draw(DATASET_PATHS)
    tail = draw(DESCENDANT_TAILS)
    descendant = "/".join([ancestor, *tail])
    construction = f"ancestor-and-descendant-{len(tail)}-deep"
    if draw(st.booleans()):
        return (ancestor, descendant, True, construction)
    return (descendant, ancestor, True, f"{construction}-reversed")


@st.composite
def root_pairs(draw: st.DrawFn) -> PairCase:
    path = draw(DATASET_PATHS)
    if draw(st.booleans()):
        return ("/", path, True, "root-contains-absolute")
    return (path, "/", True, "absolute-under-root")


@st.composite
def segment_boundary_pairs(draw: st.DrawFn) -> PairCase:
    path = draw(DATASET_PATHS)
    suffix = draw(SEGMENT_SUFFIXES)
    extended = path + suffix
    if draw(st.booleans()):
        return (path, extended, False, "final-segment-extended")
    return (extended, path, False, "final-segment-extended-reversed")


@st.composite
def sibling_pairs(draw: st.DrawFn) -> PairCase:
    parent = "/".join(["/mnt", *draw(st.lists(SEGMENTS, min_size=0, max_size=2))])
    left_component = draw(SEGMENTS)
    right_component = draw(SEGMENTS)
    assume(left_component != right_component)
    left = "/".join([parent, left_component, *draw(st.lists(SEGMENTS, max_size=2))])
    right = "/".join([parent, right_component, *draw(st.lists(SEGMENTS, max_size=2))])
    if draw(st.booleans()):
        return (left, right, False, "distinct-siblings")
    return (right, left, False, "distinct-siblings-reversed")


#: Every construction, for the properties that are indifferent to the verdict.
ANY_PAIR = st.one_of(
    equal_pairs(),
    descendant_pairs(),
    root_pairs(),
    segment_boundary_pairs(),
    sibling_pairs(),
)


def _verdicts(paths_overlap, cases: Sequence[PairCase]) -> List[bool]:
    """One helm invocation for the whole batch; memoized per pair."""
    return paths_overlap.batch([(case[0], case[1]) for case in cases])


def _report(case: PairCase, actual: bool) -> str:
    path_a, path_b, expected, construction = case
    return (
        f"pathsOverlap({path_a!r}, {path_b!r}) returned {actual} but the "
        f"{construction} construction requires {expected}"
    )


# ==========================================================================
# P1 — reflexivity
# ==========================================================================


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cases=st.lists(equal_pairs(), min_size=1, max_size=8))
def test_every_path_overlaps_itself(paths_overlap, cases: List[PairCase]) -> None:
    """P1: ``overlap(p, p)`` is true for every path.

    The guard compares each relabeling mount against every shared root, and the
    case that matters most in practice is the one where those are the same
    directory — the original defect. Reflexivity is what makes that case a
    detection rather than a coincidence of the containment tests.

    ``paths_overlap`` is a function-scoped fixture reused across generated
    examples on purpose: it is a stateless probe apart from its per-pair memo, and
    the memo is what keeps generation affordable.

    **Validates: Requirements 2.1, 3.8**
    """
    for case, actual in zip(cases, _verdicts(paths_overlap, cases)):
        assert actual is True, _report(case, actual)


# ==========================================================================
# P2 — the root
# ==========================================================================


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cases=st.lists(root_pairs(), min_size=1, max_size=8))
def test_root_overlaps_every_absolute_path(
    paths_overlap, cases: List[PairCase]
) -> None:
    """P2: ``/`` overlaps every absolute path, in either argument order.

    ``clean`` leaves the root as ``"/"``, whose ``"%s/"`` prefix is ``"//"`` and
    matches nothing, so this property is carried entirely by the extra
    ``trimSuffix "/"``. No values file can set ``datasetRoot`` to ``/`` —
    ``_validate.tpl`` requires it below ``/mnt/`` — so this is a property of the
    helper rather than of any reachable configuration, which is exactly why it
    needs asserting: nothing else would notice if it broke.

    **Validates: Requirements 2.1, 3.8**
    """
    for case, actual in zip(cases, _verdicts(paths_overlap, cases)):
        assert actual is True, _report(case, actual)


# ==========================================================================
# P3 — ancestor and descendant
# ==========================================================================


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cases=st.lists(descendant_pairs(), min_size=1, max_size=8))
def test_a_path_overlaps_everything_below_it(
    paths_overlap, cases: List[PairCase]
) -> None:
    """P3: ``overlap(p, p + "/" + tail)`` is true for any non-empty ``tail``.

    Generated at depths one to three, in both argument orders, so a relabeling
    volume placed on a parent of the routing path is caught as surely as one
    placed on a grandchild of it. A missed containment is the failure that ships
    the original defect again.

    **Validates: Requirements 2.1, 3.8**
    """
    for case, actual in zip(cases, _verdicts(paths_overlap, cases)):
        assert actual is True, _report(case, actual)


# ==========================================================================
# P4 — segment boundary
# ==========================================================================


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cases=st.lists(segment_boundary_pairs(), min_size=1, max_size=8))
def test_extending_a_final_segment_never_creates_an_overlap(
    paths_overlap, cases: List[PairCase]
) -> None:
    """P4: ``overlap(p, p + x)`` is false for a suffix ``x`` that adds no segment.

    ``x`` is a single path component, never containing a separator. "Does not
    begin with a separator" is NOT a sufficient constraint and the difference is
    the interesting part: ``x = "z/.."`` gives ``p + x = "/mnt/geodataz/.."``,
    which ``clean`` resolves to ``/mnt`` — a genuine ancestor of ``p``, so it
    genuinely overlaps. A suffix that can climb would make the property false
    about its own input rather than about the helper, so the strategy excludes
    separators outright instead of assuming them away.

    This is the property a naive ``hasPrefix $a $b`` violates, and 5.2 established
    it as the one such an implementation gets wrong: it reads
    ``/mnt/geodata-geocoding`` as a child of ``/mnt/geodata`` and refuses a
    configuration that is perfectly correct.

    **Validates: Requirements 2.1, 3.8**
    """
    for case, actual in zip(cases, _verdicts(paths_overlap, cases)):
        assert actual is False, _report(case, actual)


# ==========================================================================
# P5 — distinct siblings
# ==========================================================================


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cases=st.lists(sibling_pairs(), min_size=1, max_size=8))
def test_subtrees_that_diverge_never_overlap(
    paths_overlap, cases: List[PairCase]
) -> None:
    """P5: distinct children of a common parent do not overlap, at any depth.

    The two paths share a parent and then take differently named components, so
    neither can contain the other however deep the tails run. This is the
    over-rejection side of the guard: the design deliberately leaves room for a
    future relabeling volume on a dedicated sibling path, and a guard that
    rejected siblings would make that arrangement uninstallable.

    **Validates: Requirements 2.1, 3.8**
    """
    for case, actual in zip(cases, _verdicts(paths_overlap, cases)):
        assert actual is False, _report(case, actual)


# ==========================================================================
# P6 — symmetry
# ==========================================================================


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    cases=st.lists(ANY_PAIR, min_size=1, max_size=6),
    recipes=st.lists(DENORMALIZER_NAMES, min_size=2, max_size=2),
)
def test_swapping_the_arguments_never_changes_the_verdict(
    paths_overlap, cases: List[PairCase], recipes: List[str]
) -> None:
    """P6: ``overlap(a, b) == overlap(b, a)``, over generated and denormalized pairs.

    ``test_paths_overlap.py`` already sweeps the fixed table for symmetry. This
    differs in two ways that matter: the pairs come from every construction in
    this module rather than from a written list, and each operand is denormalized
    independently, so pairs where the two sides are spelled differently from one
    another are generated — a shape the table never contains.

    Symmetry is what makes the helper's two containment directions a single
    complete check rather than a pair of half-checks. Neither call site in the
    guard guarantees which path arrives first.

    **Validates: Requirements 2.1, 3.8**
    """
    recipe_a, recipe_b = recipes
    denormalize_a, denormalize_b = DENORMALIZERS[recipe_a], DENORMALIZERS[recipe_b]

    spellings: List[Tuple[str, str]] = []
    for path_a, path_b, _, _ in cases:
        spelled_a, spelled_b = denormalize_a(path_a), denormalize_b(path_b)
        _assert_spelling_only(path_a, spelled_a, recipe_a)
        _assert_spelling_only(path_b, spelled_b, recipe_b)
        spellings.append((spelled_a, spelled_b))

    reversed_spellings = [(path_b, path_a) for path_a, path_b in spellings]
    verdicts = paths_overlap.batch(spellings + reversed_spellings)
    forward, reverse = verdicts[: len(spellings)], verdicts[len(spellings) :]

    asymmetric = [
        (pair, first, second)
        for pair, first, second in zip(spellings, forward, reverse)
        if first != second
    ]
    assert not asymmetric, (
        f"pathsOverlap disagreed with itself under argument order on: {asymmetric} "
        f"(spellings: a={recipe_a}, b={recipe_b})"
    )


# ==========================================================================
# P7 — normalization invariance
# ==========================================================================


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    cases=st.lists(ANY_PAIR, min_size=1, max_size=6),
    recipes=st.lists(DENORMALIZER_NAMES, min_size=2, max_size=2),
)
def test_respelling_either_path_never_changes_the_verdict(
    paths_overlap, cases: List[PairCase], recipes: List[str]
) -> None:
    """P7: the verdict depends on the directories named, not on their spelling.

    Each operand is independently rewritten with one of the denormalizers —
    duplicate separators, a trailing separator, interleaved ``.`` components,
    ``x/..`` round trips, or all of them at once — and the verdict must be
    unchanged. Both the normalized and the respelled batch go into a single
    ``paths_overlap.batch`` call, so an example still costs one helm invocation.

    This is the property that the ``clean`` step exists for, and per the mutation
    matrix above it is the only one that catches ``clean``'s removal: every other
    property builds its paths already normalized, so an unnormalized helper
    answers all of them correctly. The construction's expected verdict is
    asserted too, so a helper that is consistently wrong cannot pass by being
    consistently wrong in both spellings.

    **Validates: Requirements 2.1, 3.8**
    """
    recipe_a, recipe_b = recipes
    denormalize_a, denormalize_b = DENORMALIZERS[recipe_a], DENORMALIZERS[recipe_b]

    normalized: List[Tuple[str, str]] = []
    respelled: List[Tuple[str, str]] = []
    for path_a, path_b, _, _ in cases:
        spelled_a, spelled_b = denormalize_a(path_a), denormalize_b(path_b)
        _assert_spelling_only(path_a, spelled_a, recipe_a)
        _assert_spelling_only(path_b, spelled_b, recipe_b)
        normalized.append((path_a, path_b))
        respelled.append((spelled_a, spelled_b))

    verdicts = paths_overlap.batch(normalized + respelled)
    before, after = verdicts[: len(cases)], verdicts[len(cases) :]

    for case, normalized_verdict, respelled_verdict, spelling in zip(
        cases, before, after, respelled
    ):
        expected = case[2]
        assert normalized_verdict is expected, _report(case, normalized_verdict)
        assert respelled_verdict is normalized_verdict, (
            f"pathsOverlap{spelling} returned {respelled_verdict} while "
            f"pathsOverlap({case[0]!r}, {case[1]!r}) returned "
            f"{normalized_verdict}; the two spell the same pair of directories "
            f"(a={recipe_a}, b={recipe_b}, construction={case[3]})"
        )

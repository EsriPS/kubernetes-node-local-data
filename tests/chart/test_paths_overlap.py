"""Normalization unit cases for the ``node-local-data.pathsOverlap`` helper.

The helper is the comparison the forward overlap guard in ``_validate.tpl`` runs:
given two host paths it emits the string ``"true"`` when they are the same
directory or when either contains the other, and the empty string otherwise. It
is what decides whether a relabeling mount would land on the shared routing
tree, so its normalization is the part of the guard that can be wrong while the
guard still looks right.

What the helper does, and what each part of it is load-bearing for:

* ``clean`` on both operands — duplicate separators collapsed, ``.`` and ``..``
  resolved lexically, a trailing separator stripped — so ``/mnt//geodata`` and
  ``/mnt/geodata/x/../`` name the directory they actually mean. Without it,
  ``/mnt/geodata/../other`` reads as a child of ``/mnt/geodata`` and is rejected
  as an overlap it is not.
* a further ``trimSuffix "/"``, which changes nothing except the root: ``clean``
  leaves ``"/"`` as ``"/"``, and trimming it to ``""`` is what makes the root
  contain every absolute path instead of matching nothing.
* an explicit trailing separator on the candidate prefix, which keeps the
  comparison on segment boundaries. This is the one a naive implementation gets
  wrong: a bare string-prefix test calls ``/mnt/geodata-geocoding`` a child of
  ``/mnt/geodata``.
* containment tested in **both** directions, so a relabeling volume placed on a
  parent of the routing path is caught as well as one placed on a child.

LEXICAL ONLY — DO NOT OVER-TRUST A GREEN RUN
--------------------------------------------
This comparison is lexical, and cannot be anything else: ``helm template`` runs
nowhere near a node and cannot resolve symlinks, bind mounts, or hard links. Two
distinct paths that alias the same inodes pass every case in this file and still
break at runtime, because SELinux labels live on inodes rather than on paths.
Distinct paths are necessary, not sufficient; the operator must also ensure
distinct inodes. See ``tuning/gotchas.md``.

The wider scope note applies here as it does to every chart test touching this
defect: a real SELinux relabel cannot be reproduced by ``helm template``. These
cases prove that the configuration expressing the bug is unrepresentable, and
nothing more. The runtime behaviour is covered by the recorded live evidence in
the spec directory. **A green chart suite is never proof that the relabel is
fixed.**

The helper is invoked directly through the probe chart built in ``conftest.py``:
a minimal scratch chart carrying a verbatim copy of ``_validate.tpl`` plus one
template that calls the helper the way the guard calls it. The guard itself, and
the failure message it produces, are covered in ``test_render.py``.

**Validates: Requirements 2.1, 3.8**
"""

from typing import Tuple

import pytest


# Each case names the element of the helper's logic it is falsifiable against.
# Established by mutation, over scratch copies of _validate.tpl in a temporary
# directory and never the file in the chart. Every one of these mutants is caught
# by at least one case below:
#
#   drop `clean`                 both operands used raw
#   drop `trimSuffix "/"`        `clean` output used as-is
#   drop the prefix separator    hasPrefix $a $b instead of hasPrefix "$a/" $b
#   drop the a-contains-b direction
#   drop the b-contains-a direction
#   drop `eq`                    containment tests only
#   always true                  the degenerate mutant every negative case pins
#
# One result is worth recording because it is not what you would guess: the
# trailing-separator cases do NOT catch either normalizer. `hasPrefix "$a/" $b`
# with a = "/mnt/geodata/" matches b = "/mnt/geodata/" as its own prefix, so an
# unnormalized trailing separator is absorbed by the containment test. Those
# cases are falsifiable against `eq` instead. The normalizers are pinned by the
# duplicate-separator, `..`, and root cases, which no other part of the condition
# rescues.

OVERLAPPING_PAIRS: Tuple[Tuple[str, str, str, str], ...] = (
    # id, path_a, path_b, mutation the case catches
    ("identical", "/mnt/geodata", "/mnt/geodata", "drop eq"),
    (
        "trailing-separator-on-a",
        "/mnt/geodata/",
        "/mnt/geodata",
        "drop eq",
    ),
    (
        "trailing-separator-on-b",
        "/mnt/geodata",
        "/mnt/geodata/",
        "drop eq",
    ),
    # The duplicate separator sits inside the shared portion, so no amount of
    # prefix matching rescues it: without `clean` these are simply two different
    # strings and the guard would wave the mount through.
    ("duplicate-separator-in-prefix", "/mnt//geodata", "/mnt/geodata", "drop clean"),
    (
        "duplicate-separator-in-child",
        "/mnt/geodata//sub",
        "/mnt/geodata",
        "drop the b-contains-a direction",
    ),
    (
        "dot-dot-resolved-to-a-child",
        "/mnt/geodata/x/../sub",
        "/mnt/geodata",
        "drop the b-contains-a direction",
    ),
    (
        "parent-then-child",
        "/mnt/geodata",
        "/mnt/geodata/geocoding",
        "drop the a-contains-b direction",
    ),
    (
        "child-then-parent",
        "/mnt/geodata/geocoding",
        "/mnt/geodata",
        "drop the b-contains-a direction",
    ),
    (
        "grandchild",
        "/mnt/geodata",
        "/mnt/geodata/geocoding/locators",
        "drop the a-contains-b direction",
    ),
    # `clean` leaves the root as "/", whose "%s/" prefix is "//" and matches
    # nothing. The extra trimSuffix is the only reason the root behaves like the
    # ancestor of everything it is. These two are also the only cases that pin
    # that step, in either direction.
    ("root-contains-absolute", "/", "/mnt/geodata", 'drop trimSuffix "/"'),
    ("absolute-under-root", "/mnt/geodata", "/", 'drop trimSuffix "/"'),
    ("root-identical", "/", "/", "drop eq"),
)

NON_OVERLAPPING_PAIRS: Tuple[Tuple[str, str, str, str], ...] = (
    # id, path_a, path_b, mutation the case catches
    #
    # The segment-boundary cases. These are the ones the naive prefix test gets
    # wrong, and the reason the guard appends a separator before comparing.
    ("numeric-suffix-sibling", "/mnt/geodata", "/mnt/geodata2", "drop the prefix separator"),
    (
        "dashed-suffix-sibling",
        "/mnt/geodata",
        "/mnt/geodata-geocoding",
        "drop the prefix separator",
    ),
    (
        "dashed-suffix-sibling-reversed",
        "/mnt/geodata-geocoding",
        "/mnt/geodata",
        "drop the prefix separator",
    ),
    (
        "child-of-a-suffix-sibling",
        "/mnt/geodata2/sub",
        "/mnt/geodata",
        "drop the prefix separator",
    ),
    # `..` that leaves the tree. Without `clean` this reads as a child of
    # /mnt/geodata and is rejected as an overlap, which would make the guard
    # refuse a perfectly good configuration.
    ("dot-dot-escapes-the-tree", "/mnt/geodata/../other", "/mnt/geodata", "drop clean"),
    ("unrelated", "/mnt/geodata", "/mnt/other", "always true"),
    ("unrelated-mount-points", "/mnt/geodata", "/srv/geodata", "always true"),
)


@pytest.mark.parametrize(
    ("path_a", "path_b", "catches"),
    [case[1:] for case in OVERLAPPING_PAIRS],
    ids=[case[0] for case in OVERLAPPING_PAIRS],
)
def test_paths_that_name_the_same_tree_overlap(
    paths_overlap, path_a: str, path_b: str, catches: str
) -> None:
    """Equal, contained, and denormalized-but-equivalent paths must overlap.

    A missed overlap is the failure that ships the original defect again: the
    guard accepts a relabeling mount over the routing tree and the first pod to
    consume it restamps the tree with its own MCS categories.

    **Validates: Requirements 2.1, 3.8**
    """
    assert paths_overlap(path_a, path_b) is True, (
        f"pathsOverlap({path_a!r}, {path_b!r}) must be true; this case is what "
        f"catches: {catches}"
    )


@pytest.mark.parametrize(
    ("path_a", "path_b", "catches"),
    [case[1:] for case in NON_OVERLAPPING_PAIRS],
    ids=[case[0] for case in NON_OVERLAPPING_PAIRS],
)
def test_paths_that_name_different_trees_do_not_overlap(
    paths_overlap, path_a: str, path_b: str, catches: str
) -> None:
    """Distinct directories must not overlap, siblings sharing a prefix included.

    A false overlap is the quieter failure: the guard over-rejects and a valid
    dedicated-path configuration cannot be installed at all, which is exactly
    the arrangement the design leaves open for a future relabeling volume.

    **Validates: Requirements 2.1, 3.8**
    """
    assert paths_overlap(path_a, path_b) is False, (
        f"pathsOverlap({path_a!r}, {path_b!r}) must be false; this case is what "
        f"catches: {catches}"
    )


def test_overlap_is_symmetric_across_the_whole_case_table(paths_overlap) -> None:
    """Swapping the arguments must never change the verdict.

    Both call sites in the guard iterate a mount set against a shared-root set,
    and neither guarantees which path arrives first. Symmetry is what makes the
    two containment directions equivalent to one another rather than a pair of
    half-checks, so it is asserted over the table as a whole rather than trusted
    from the two reversed cases above.

    **Validates: Requirements 2.1, 3.8**
    """
    pairs = [case[1:3] for case in OVERLAPPING_PAIRS + NON_OVERLAPPING_PAIRS]
    forward = paths_overlap.batch(pairs)
    reverse = paths_overlap.batch([(path_b, path_a) for path_a, path_b in pairs])

    asymmetric = [
        (pair, first, second)
        for pair, first, second in zip(pairs, forward, reverse)
        if first != second
    ]
    assert not asymmetric, f"pathsOverlap disagreed with itself on: {asymmetric}"


def test_degenerate_relative_inputs_are_normalized_rather_than_special_cased(
    paths_overlap,
) -> None:
    """Documented, not contracted: what the helper does with degenerate input.

    ``clean("")`` is ``"."``, so the empty string and ``"."`` are one path and
    overlap, while the empty string does not overlap an absolute path. Neither
    input is reachable from the chart — every path the guard compares comes from
    ``datasetRoot``, which ``_validate.tpl`` already requires to be below
    ``/mnt/``, or from the mount inventory, which is built from it. This case is
    here so the behaviour is recorded rather than rediscovered, and it doubles as
    a second falsification of the ``clean`` step: without it, ``""`` and ``"."``
    are two different strings that contain neither the other.

    Read it as an observation about the helper, not as a requirement on it. If a
    future caller genuinely needs relative paths, that is a design question for
    the spec, not something to settle by reading this test.

    **Validates: Requirements 2.1, 3.8**
    """
    assert paths_overlap("", "") is True
    assert paths_overlap("", ".") is True
    assert paths_overlap(".", "") is True
    assert paths_overlap("", "/mnt/geodata") is False

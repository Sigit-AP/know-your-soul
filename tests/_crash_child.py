"""Crash-injection child process for ``tests/test_power_loss_recovery.py``.

Not a test module. Spawned as a real subprocess; it builds a genuine
projection (real SQLite store, real importer, real projector, real fsyncs)
and then dies with ``os._exit(137)`` — the SIGKILL signature — at one named
point of the publish/backup path. The parent test then reopens the store and
projection exactly like a post-crash restart would and asserts that no
partial artifact is ever served and that recovery converges.

Usage: ``python tests/_crash_child.py <workdir> <point> [extra]``

Points:
  baseline              build + activate generation 1, exit 0 (sanity).
  mid-artifact-write    die after the 2nd artifact rename of generation 2.
  partial-tmp           die leaving a half-written ``graph.json.tmp-<pid>``
                        (the real temp path, real write, cut short).
  before-pointer-rename die after the store CAS commit, before the CURRENT
                        rename (the rename never happens).
  after-pointer-rename  die immediately after the CURRENT rename, before the
                        directory fsync.
  mid-backup-staging    die while staging a protected-set backup, before the
                        activating rename (extra = destination dir).
  mid-backup-activation die between the old-backup rename-aside and the
                        activating rename (extra = destination dir).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ALPHA = b"# Alpha Lessons\n\n## First Lesson\n\nAlways roll the database back before retrying.\n"
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"
GAMMA = b"# Gamma\n\n## Standing Order\n\nNever widen a transaction to cover reads.\n"


def _die() -> None:
    os._exit(137)  # SIGKILL signature: no atexit, no finally, no cleanup


def main() -> int:
    workdir = Path(sys.argv[1])
    point = sys.argv[2]
    extra = sys.argv[3] if len(sys.argv) > 3 else ""

    from know_your_soul.backup import backup_protected_artifacts
    from know_your_soul.domain import PrimaryDomain
    from know_your_soul.importer import SeedImporter
    from know_your_soul.projection import GraphProjector
    from know_your_soul.store import EventStore

    store = EventStore(workdir / "soul.sqlite3")
    corpus = workdir / "corpus"
    corpus.mkdir(exist_ok=True)
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer = SeedImporter(store, state_dir=workdir / "import-state")
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=workdir / "out")
    projector.rebuild()  # generation 1 activated cleanly

    if point == "mid-backup-staging":
        real_replace = os.replace

        def die_on_activation(src: object, dst: object) -> None:
            if os.path.basename(str(dst)) == "soul.db":
                _die()  # crash mid-staging: the activation swap never begins
            real_replace(str(src), str(dst))

        os.replace = die_on_activation  # type: ignore[assignment]
        backup_protected_artifacts(store, projection_root=workdir / "out", dest_dir=extra)
        return 0

    if point == "mid-backup-activation":
        real_replace = os.replace

        def die_on_swap(src: object, dst: object) -> None:
            if str(dst) == extra:
                _die()  # old backup renamed aside; the new one never lands
            real_replace(str(src), str(dst))

        os.replace = die_on_swap  # type: ignore[assignment]
        backup_protected_artifacts(store, projection_root=workdir / "out", dest_dir=extra)
        return 0

    # Every remaining point publishes a second, different generation.
    if point != "baseline":
        (corpus / "gamma.md").write_bytes(GAMMA)
        importer.import_file(corpus / "gamma.md", domain=PrimaryDomain.P2, source_uri="soul://corpus/gamma.md")

    if point == "mid-artifact-write":
        real_replace = os.replace
        renames = {"count": 0}

        def die_on_second_artifact(src: object, dst: object) -> None:
            dst_path = Path(str(dst))
            if len(dst_path.parent.name) == 64 and dst_path.parent.parent.name == "generations":
                renames["count"] += 1
                if renames["count"] == 2:
                    _die()  # one artifact renamed; the second temp never lands
            real_replace(str(src), str(dst))

        os.replace = die_on_second_artifact  # type: ignore[assignment]
        projector.rebuild()
    elif point == "partial-tmp":
        real_write = GraphProjector._write_if_changed

        def patched_partial(self: GraphProjector, path: Path, data: bytes) -> bool:
            if path.name == "graph.json":
                # Crash mid-write: the real temp path gets half the bytes via a
                # real write + flush; the destination rename never happens.
                tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
                with open(tmp, "wb") as handle:
                    handle.write(data[: len(data) // 2])
                    handle.flush()
                _die()
            return real_write(self, path, data)

        GraphProjector._write_if_changed = patched_partial  # type: ignore[method-assign]
        projector.rebuild()
    elif point == "before-pointer-rename":
        real_replace = os.replace

        def die_before_rename(src: object, dst: object) -> None:
            if os.path.basename(str(dst)) == "CURRENT":
                _die()  # store CAS committed; the CURRENT rename never happens
            real_replace(str(src), str(dst))

        os.replace = die_before_rename  # type: ignore[assignment]
        projector.rebuild()
    elif point == "after-pointer-rename":
        real_replace = os.replace

        def die_after_rename(src: object, dst: object) -> None:
            real_replace(str(src), str(dst))
            if os.path.basename(str(dst)) == "CURRENT":
                _die()  # rename landed; the directory fsync never happens

        os.replace = die_after_rename  # type: ignore[assignment]
        projector.rebuild()
    elif point != "baseline":
        raise SystemExit(f"unknown crash point: {point}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

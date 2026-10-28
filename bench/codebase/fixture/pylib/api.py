"""Ground-truth consumer of pylib.core."""

from .core import Worker, base_helper


def handle() -> int:
    """Calls Worker.run via an instance (ground truth: handle -> Worker.run)."""
    worker = Worker()
    return worker.run()


def direct() -> int:
    """Calls base_helper directly (ground truth: direct -> base_helper)."""
    return base_helper(1)

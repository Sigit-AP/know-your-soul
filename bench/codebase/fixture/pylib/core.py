"""Tiny hand-written library: the ground-truth anchor of the codebase bench."""


def base_helper(x: int) -> int:
    """Ground-truth callee: called by Worker.run (core.py) and direct (api.py)."""
    return x + 1


class Worker:
    """Ground-truth class; Worker.run calls base_helper."""

    def run(self) -> int:
        return base_helper(41)

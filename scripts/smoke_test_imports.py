"""Import every top-level module so packaging issues surface immediately."""

from __future__ import annotations


def main() -> None:
    # Order roughly mirrors the dependency graph.
    from go2_local_brain import __version__  # noqa: F401
    from go2_local_brain import config  # noqa: F401
    from go2_local_brain.safety import limits  # noqa: F401
    from go2_local_brain.driver import webrtc_client  # noqa: F401
    from go2_local_brain.brain import local_llm  # noqa: F401
    from go2_local_brain.viz import rerun_logger  # noqa: F401
    from go2_local_brain import main as _main  # noqa: F401

    print("imports ok")


if __name__ == "__main__":
    main()

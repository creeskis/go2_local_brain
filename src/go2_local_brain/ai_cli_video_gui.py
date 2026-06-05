"""AI CLI + live video, with WASD/QE keyboard movement enabled."""

from __future__ import annotations

from .mode_gui import GuiMode, make_main


MODE = GuiMode(
    title="Go2 AI CLI + Video",
    enable_ai=True,
    enable_keyboard=True,
    enable_lidar=False,
    show_drive_panel=False,
)


def main() -> None:
    make_main(MODE, default_port=8771)


if __name__ == "__main__":
    main()

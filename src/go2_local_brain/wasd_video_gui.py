"""WASD/QE keyboard controller + live video."""

from __future__ import annotations

from .mode_gui import GuiMode, make_main


MODE = GuiMode(
    title="Go2 WASD + Video",
    enable_ai=False,
    enable_keyboard=True,
    enable_lidar=False,
    show_drive_panel=True,
)


def main() -> None:
    make_main(MODE, default_port=8773)


if __name__ == "__main__":
    main()

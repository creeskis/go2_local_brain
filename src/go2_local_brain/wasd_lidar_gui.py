"""WASD/QE keyboard controller + live video + LiDAR."""

from __future__ import annotations

from .mode_gui import GuiMode, make_main


MODE = GuiMode(
    title="Go2 WASD + Video + LiDAR",
    enable_ai=False,
    enable_keyboard=True,
    enable_lidar=True,
    show_drive_panel=True,
)


def main() -> None:
    make_main(MODE, default_port=8774)


if __name__ == "__main__":
    main()

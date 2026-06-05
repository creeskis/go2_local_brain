"""AI CLI + live video + LiDAR, without keyboard driving controls."""

from __future__ import annotations

from .mode_gui import GuiMode, make_main


MODE = GuiMode(
    title="Go2 AI + Video + LiDAR",
    enable_ai=True,
    enable_keyboard=False,
    enable_lidar=True,
    show_drive_panel=False,
)


def main() -> None:
    make_main(MODE, default_port=8772)


if __name__ == "__main__":
    main()

"""Export the generated PNG artwork as a multi-resolution Windows ICO.

This only performs format conversion and high-quality downsampling. The design
is the original imagegen artwork, not a Python-drawn substitute.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def export_windows_icon(source: Path, destination: Path, *, replace: bool = False) -> None:
    if destination.exists() and not replace:
        raise FileExistsError("Icon already exists; pass --replace to regenerate it.")
    artwork = QImage(str(source))
    if artwork.isNull() or artwork.width() != artwork.height():
        raise ValueError("A readable square PNG is required.")
    frames = []
    for size in ICON_SIZES:
        frame = artwork.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not frame.save(buffer, "PNG"):
            raise ValueError("Could not encode an icon frame.")
        frames.append(bytes(encoded))
        buffer.close()
    offset = 6 + 16 * len(frames)
    entries = []
    for size, frame in zip(ICON_SIZES, frames):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                   1, 32, len(frame), offset))
        offset += len(frame)
    destination.write_bytes(struct.pack("<HHH", 0, 1, len(frames))
                            + b"".join(entries) + b"".join(frames))
    print(f"Created {destination} ({len(frames)} resolutions, 16-256 px).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    assets = Path(__file__).resolve().parents[1] / "app" / "assets"
    export_windows_icon(assets / "student-assistant-butter-v1.png",
                        assets / "student-assistant-butter-v1.ico", replace=args.replace)


if __name__ == "__main__":
    main()

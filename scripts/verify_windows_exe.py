from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

import pefile


def resource_types(path: Path) -> set[int]:
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        directory = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        if directory is None:
            return set()
        return {
            entry.struct.Id
            for entry in directory.entries
            if entry.name is None
        }
    finally:
        pe.close()


def group_icon_layer_count(path: Path) -> int:
    pe = pefile.PE(str(path), fast_load=False)
    try:
        root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        if root is None:
            return 0
        for resource_type in root.entries:
            if resource_type.name is not None or (
                resource_type.struct.Id != pefile.RESOURCE_TYPE["RT_GROUP_ICON"]
            ):
                continue
            counts: list[int] = []
            for resource_name in resource_type.directory.entries:
                for language in resource_name.directory.entries:
                    data = language.data.struct
                    blob = pe.get_data(data.OffsetToData, data.Size)
                    if len(blob) >= 6:
                        _reserved, icon_type, count = struct.unpack_from("<HHH", blob)
                        if icon_type == 1:
                            counts.append(count)
            return max(counts, default=0)
        return 0
    finally:
        pe.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    arguments = parser.parse_args()
    path = arguments.executable.resolve()
    if not path.is_file():
        raise SystemExit(f"Executable not found: {path}")
    types = resource_types(path)
    if pefile.RESOURCE_TYPE["RT_GROUP_ICON"] not in types:
        raise SystemExit("Windows executable does not contain a group icon resource")
    if pefile.RESOURCE_TYPE["RT_MANIFEST"] not in types:
        raise SystemExit("Windows executable does not contain the DPI manifest")
    icon_layers = group_icon_layer_count(path)
    if icon_layers < 4:
        raise SystemExit(
            f"Windows executable icon has too few embedded image layers: {icon_layers}"
        )
    print(f"PATH={path}")
    print(f"SHA256={sha256(path)}")
    print(f"ICON=verified ({icon_layers} layers)")
    print("MANIFEST=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

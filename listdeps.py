#!/usr/bin/env python
"""
To test:

Assume the opened blend file sits in the project root:

$ blender -b batter-tests/root/scene.blend -P listdeps.py

Explicitly provide a root path:

$ blender -b batter-tests/root/scene.blend -P listdeps.py -- -r /some/other/root

"""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import bpy
from bpy.types import Library

# Ensure Batter can be imported, even when it's not installed as package.
_my_dir = Path(__file__).resolve().parent
if str(_my_dir) not in sys.path:
    sys.path.append(str(_my_dir))

# Import not at top of file, but has to be below the modification sys.path.
from batter import asset_usage as au  # noqa: E402


@dataclasses.dataclass
class CLIArgs:
    root_path: Path
    json: bool


# Will be updated by actually parsing CLI arguments.
cli_args = CLIArgs(
    root_path=Path(".").resolve(),
    json=False,
)


class JSONEncoder(json.JSONEncoder):
    def default(self, o: object) -> object:
        # `dataclasses.is_dataclass(o)` also returns True if `o` is a dataclass class.
        # This code only supports class instances, though.
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, set):
            return tuple(o)
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, Library):
            return f"<library {o.filepath}>"
        return super().default(o)


def main():
    global cli_args
    cli_args = parse_cli_args()

    asset_usages = au.find()

    if cli_args.json:
        # Convert Library objects to paths.
        for_json = {str(au.library_abspath(lib)): v for lib, v in asset_usages.items()}
        json.dump(
            for_json,
            sys.stdout,
            cls=JSONEncoder,
            indent="  ",
        )
        return

    print()
    header = f"Dependencies, relative to {cli_args.root_path}:"
    print(len(header) * "-")
    print(header)
    print(len(header) * "-")
    libs = sorted(bpy.data.libraries, key=lambda lib: lib.filepath)
    for lib in [None, *libs]:
        # Show the blend file itself:
        lib_path = au.library_abspath(lib)
        print(f"\033[96m{colourise_and_simplify(lib_path)}\033[0m")

        # Show what this blend file uses:
        links_to = asset_usages.get(lib, [])
        for asset_usage in links_to:
            color = 35 if asset_usage.is_blendfile else 90
            print(
                f"  \033[{color}m{colourise_and_simplify(asset_usage.abspath, 58)}",
                f" ({asset_usage.reference_path!r})\033[0m",
            )

    print(len(header) * "-")


def relative_to_root(path: Path | str) -> Path:
    """Return a relative path if it can be made relative to 'root_path'.

    If not, return an absolute path.
    """

    assert cli_args.root_path.is_dir()

    if isinstance(path, str):
        path = Path(bpy.path.abspath(path))

    resolved = path.resolve()
    try:
        return resolved.relative_to(cli_args.root_path)
    except ValueError:
        pass

    # See how many common parts there are.
    if resolved.anchor != cli_args.root_path.anchor:
        # Different 'anchor' means constructing a relative path is impossible.
        return resolved

    levels_up = 0
    root_up = cli_args.root_path
    while root_up.parents:
        levels_up += 1
        root_up = root_up.parent

        try:
            relative = resolved.relative_to(root_up)
        except ValueError:
            pass
        else:
            return Path(levels_up * "../") / relative

    return resolved


def colourise_and_simplify(asset_path: Path, width=0) -> str:
    if asset_path.is_absolute():
        abspath = asset_path
        asset_path = relative_to_root(abspath)
    else:
        abspath = cli_args.root_path / asset_path

    if width:
        as_string = f"{asset_path!s:.<{width}}"
    else:
        as_string = str(asset_path)

    if abspath.exists():
        return as_string
    return f"\033[91m{as_string}\033[0m"


def parse_cli_args() -> CLIArgs:
    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]
    else:
        argv = []

    current_blendfile_dir = Path(bpy.data.filepath).resolve().parent

    my_name = Path(__file__).name
    parser = argparse.ArgumentParser(my_name)
    parser.add_argument("-r", "--root", type=Path, default=current_blendfile_dir)
    parser.add_argument("-j", "--json", action="store_true", default=False)
    args = parser.parse_args(argv)

    return CLIArgs(
        root_path=args.root.resolve(),
        json=args.json,
    )


if __name__ == "__main__":
    main()

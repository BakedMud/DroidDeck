#!/usr/bin/python3
"""Select a staged DXVK payload for one DroidDeck Proton game launch.

Place this script in the system Vulkan runtime directory beside a DXVK payload.
The game launcher selects it using DROIDDECK_SYSTEM_VULKAN_RUNTIME and
DROIDDECK_SYSTEM_VULKAN_DXVK=managed; DROIDDECK_HYBRIS_DXVK selects the payload
subdirectory (default: dxvk). Payloads can use x32/x64 or Proton's
i386-windows/x86_64-windows/aarch64-windows directories.
Run `python3 run-proton.py --check /path/to/proton` to check the payload and
Proton source layout without starting Proton or changing a game prefix.

This wrapper does not install the graphics runtime, select Vulkan layers,
change synchronization settings, or provide game-specific workarounds.
"""

import argparse
import os
from pathlib import Path
import struct
import sys


DLL_NAMES = ("d3d8.dll", "d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll")
PE_MACHINES = {"i386-windows": 0x014c, "x86_64-windows": 0x8664, "aarch64-windows": 0xaa64}
PREFIX_MARKER = "droiddeck-dxvk-profile-v1"


def check_machine(dll, expected):
    """Reject a misplaced architecture before Proton changes the prefix."""
    with dll.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"invalid PE DXVK file: {dll}")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset < 64:
            raise ValueError(f"invalid PE DXVK file: {dll}")
        stream.seek(offset)
        pe = stream.read(6)
        if len(pe) != 6 or pe[:4] != b"PE\0\0":
            raise ValueError(f"invalid PE DXVK file: {dll}")
        machine = struct.unpack_from("<H", pe, 4)[0]
        if machine != expected:
            raise ValueError(f"wrong DXVK machine 0x{machine:04x}, expected 0x{expected:04x}: {dll}")


def prepare(proton, dxvk):
    """Validate all inputs and compile an in-memory copy of the Proton script."""
    native_layout = dxvk.name == "dxvk-mali" or any((dxvk / arch).exists() for arch in PE_MACHINES)
    architectures = PE_MACHINES if native_layout else ("x32", "x64")
    for architecture in architectures:
        for name in DLL_NAMES:
            dll = dxvk / architecture / name
            if not dll.is_file():
                raise ValueError(f"missing DXVK file: {dll}")
            with dll.open("rb") as stream:
                if not stream.read(1):
                    raise ValueError(f"empty DXVK file: {dll}")
            if native_layout:
                check_machine(dll, PE_MACHINES[architecture])

    source = proton.read_text(encoding="utf-8")
    for is32, architecture in (("False", "x64"), ("True", "x32")):
        needle = f'g_proton.arch_pe_dir("wine/dxvk", {is32}) + f + ".dll"'
        if source.count(needle) != 1:
            raise ValueError(f"unsupported Proton layout: expected one {architecture} DXVK copy expression")
        if native_layout:
            # Let the selected Proton choose its architecture. A 64-bit game
            # can use native ARM64 or x86-64 depending on its runtime settings.
            directories = {arch: str(dxvk / arch) + "/" for arch in PE_MACHINES}
            selected = f'g_proton.arch_pe_dir("wine/dxvk", {is32}).rstrip("/").rsplit("/", 1)[-1]'
            replacement = repr(directories) + "[" + selected + '] + f + ".dll"'
        else:
            replacement = repr(str(dxvk / architecture) + "/") + ' + f + ".dll"'
        source = source.replace(needle, replacement, 1)

    needle = "            # check whether any prefix config has changed"
    if source.count(needle) != 1:
        raise ValueError("unsupported Proton layout: expected one prefix configuration marker")
    # The supported Proton copies DXVK DLLs on every launch. This marker records
    # profile activation; it does not need changing whenever the payload changes.
    marker = repr("\n" + PREFIX_MARKER)
    source = source.replace(needle, f"            prefix_info += {marker}\n" + needle, 1)
    return compile(source, str(proton), "exec")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate inputs without running Proton")
    parser.add_argument("proton", help="selected Proton Python script")
    parser.add_argument("proton_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.check and (len(args.proton_args) < 2 or args.proton_args[0] != "waitforexitandrun"):
        parser.error("expected: PROTON waitforexitandrun GAME [ARGUMENTS...]")

    proton = Path(args.proton)
    # A sibling payload directory by name (dxvk-2.6.2 for Xclipse); never a path.
    name = os.environ.get("DROIDDECK_HYBRIS_DXVK") or "dxvk"
    if not name.startswith("dxvk") or "/" in name or name.startswith("."):
        print(f"DroidDeck DXVK profile: invalid payload name {name!r}", file=sys.stderr)
        return 1
    dxvk = Path(__file__).resolve().parent / name
    try:
        code = prepare(proton, dxvk)
    except (OSError, ValueError, SyntaxError) as error:
        print(f"DroidDeck DXVK profile: {error}", file=sys.stderr)
        return 1

    if args.check:
        print(f"DroidDeck DXVK profile: payload present and Proton layout supported ({dxvk})")
        return 0

    print(f"DROIDDECK_GRAPHICS profile={PREFIX_MARKER} dxvk={dxvk}", flush=True)
    # Preserve the selected Proton path, including symlinks, and its arguments.
    # Proton uses argv[0] and sibling Python imports to locate its own depot.
    sys.argv = [args.proton, *args.proton_args]
    sys.path.insert(0, str(proton.absolute().parent))
    exec(code, {"__name__": "__main__", "__file__": args.proton,
                "__package__": None, "__spec__": None})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

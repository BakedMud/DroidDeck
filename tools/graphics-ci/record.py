#!/usr/bin/env python3
"""Normalize, attest, and verify source-built graphics component archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import zipfile


FORMAT = 1
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 8192
COMPONENTS = ("hybris", "dxvk", "wsi", "dxvk-xclipse", "dxvk-mali", "gamescope")
ASSET_NAMES = {
    "hybris": "runtime.tzst",
    "dxvk": "dxvk.zip",
    "wsi": "wsi.zip",
    "dxvk-xclipse": "dxvk-xclipse.tar.gz",
    "dxvk-mali": "dxvk-mali.zip",
    "gamescope": "gamescope.tzst",
}
REQUIRED_HYBRIS_LIBRARIES = (
    "lib/libhybris-vulkan-icd.so.0",
    "lib/libhybris-common.so.1",
    "lib/libhybris/linker/q.so",
    "lib/libVkLayer_hybris_compat.so",
    "lib/libVkLayer_DROIDDECK_placed_memory.so",
)
DLL_NAMES = ("d3d8.dll", "d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _object(value, description: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    return value


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    normalized = str(path)
    if path.is_absolute() or ".." in path.parts or normalized in ("", "."):
        raise ValueError(f"unsafe archive path: {name}")
    return normalized


def _check_tree(entries: dict[str, tuple[str, int, bytes | str]]) -> None:
    for name, (kind, _, target) in entries.items():
        for parent in PurePosixPath(name).parents:
            parent_name = str(parent)
            if parent_name in entries:
                raise ValueError(f"archive member below a file or symlink: {name}")
        if kind == "symlink":
            if not isinstance(target, str) or not target or "\\" in target:
                raise ValueError(f"unsafe archive symlink: {name}")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            _safe_name(resolved)
            if resolved not in entries:
                raise ValueError(f"archive symlink target is missing: {name}")


def _bounded_zstd(data: bytes) -> bytes:
    try:
        process = subprocess.Popen(
            ["zstd", "-q", "-dc"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ValueError("zstd is required to inspect .tzst component archives") from error
    assert process.stdin is not None and process.stdout is not None
    writer_errors = []

    def write_input():
        try:
            process.stdin.write(data)
        except BrokenPipeError:
            pass
        except Exception as error:  # Propagate unexpected I/O failures below.
            writer_errors.append(error)
        finally:
            process.stdin.close()

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    chunks, total = [], 0
    while block := process.stdout.read(1024 * 1024):
        total += len(block)
        if total > MAX_EXPANDED_BYTES:
            process.kill()
            process.wait()
            writer.join()
            raise ValueError("component archive exceeds expanded-size limit")
        chunks.append(block)
    error = process.stderr.read() if process.stderr else b""
    status = process.wait()
    writer.join()
    if writer_errors:
        raise ValueError("failed to stream zstd component archive") from writer_errors[0]
    if status != 0:
        raise ValueError("invalid zstd component archive: " + error.decode("utf-8", "replace").strip())
    return b"".join(chunks)


def _read_tar(data: bytes, compression: str) -> dict[str, tuple[str, int, bytes | str]]:
    if compression == "zstd":
        data = _bounded_zstd(data)
        mode = "r:"
    elif compression == "gzip":
        mode = "r:gz"
    else:
        raise ValueError(f"unsupported tar compression: {compression}")
    entries: dict[str, tuple[str, int, bytes | str]] = {}
    seen = set()
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode=mode)
        with archive:
            for index, member in enumerate(archive, 1):
                if index > MAX_ENTRIES:
                    raise ValueError("component archive has too many entries")
                if member.name in (".", "./") and member.isdir():
                    continue
                name = _safe_name(member.name)
                if name in seen:
                    raise ValueError(f"duplicate archive path: {name}")
                seen.add(name)
                if member.isdir():
                    continue
                if member.islnk():
                    raise ValueError(f"unsafe hardlink archive entry: {name}")
                if member.issym():
                    entries[name] = ("symlink", 0o777, member.linkname)
                    continue
                if not member.isfile():
                    raise ValueError(f"unsupported archive entry type: {name}")
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("component archive exceeds expanded-size limit")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"cannot read component archive entry: {name}")
                content = extracted.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError(f"truncated component archive entry: {name}")
                entries[name] = ("file", 0o755 if member.mode & 0o111 else 0o644, content)
    except (tarfile.TarError, EOFError) as error:
        raise ValueError("invalid tar component archive") from error
    _check_tree(entries)
    return entries


def _read_zip(data: bytes) -> dict[str, tuple[str, int, bytes | str]]:
    entries: dict[str, tuple[str, int, bytes | str]] = {}
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise ValueError("component archive has too many entries")
            for member in archive.infolist():
                name = _safe_name(member.filename.rstrip("/") if member.is_dir() else member.filename)
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if member.is_dir() and kind in (0, stat.S_IFDIR):
                    if name in seen:
                        raise ValueError(f"duplicate archive path: {name}")
                    seen.add(name)
                    continue
                if name in seen:
                    raise ValueError(f"duplicate archive path: {name}")
                seen.add(name)
                if kind not in (0, stat.S_IFREG):
                    raise ValueError(f"unsupported archive entry type: {name}")
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("component archive exceeds expanded-size limit")
                content = archive.read(member)
                if len(content) != member.file_size:
                    raise ValueError(f"truncated component archive entry: {name}")
                entries[name] = ("file", 0o755 if mode & 0o111 else 0o644, content)
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("invalid zip component archive") from error
    _check_tree(entries)
    return entries


def _read_archive(data: bytes, name: str) -> dict[str, tuple[str, int, bytes | str]]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("component archive exceeds compressed-size limit")
    if name.endswith(".zip"):
        return _read_zip(data)
    if name.endswith(".tar.gz"):
        return _read_tar(data, "gzip")
    if name.endswith(".tzst") or name.endswith(".tar.zst"):
        return _read_tar(data, "zstd")
    raise ValueError(f"unsupported component archive format: {name}")


def _tree_digest(entries: dict[str, tuple[str, int, bytes | str]]) -> str:
    tree = {}
    for name, (kind, mode, content) in sorted(entries.items()):
        tree[name] = [kind, mode, sha256(content) if kind == "file" else content]
    return sha256(json_bytes(tree))


def _read_entry(entries, name: str) -> bytes:
    seen = set()
    while True:
        if name in seen or len(seen) > 16:
            raise ValueError(f"component archive symlink cycle: {name}")
        seen.add(name)
        if name not in entries:
            raise ValueError(f"component archive is missing: {name}")
        kind, _, content = entries[name]
        if kind == "file":
            assert isinstance(content, bytes)
            return content
        assert isinstance(content, str)
        name = _safe_name(posixpath.normpath(posixpath.join(posixpath.dirname(name), content)))


def _env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"selected source does not support this component; missing {path}")
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid source metadata line in {path}: {line}")
        values[key] = value
    return values


def _source_release(source: Path) -> dict:
    path = source / "tools/powervr/release.json"
    if not path.is_file():
        raise ValueError("selected source does not support graphics component records; missing tools/powervr/release.json")
    release = _object(json.loads(path.read_text()), "PowerVR release template")
    if release.get("format") != 1 or not isinstance(release.get("archives"), list):
        raise ValueError("selected source has an unsupported PowerVR release template")
    return release


def _archive_template(release: dict, component: str) -> dict:
    matches = []
    for value in release["archives"]:
        if not isinstance(value, dict):
            continue
        if value.get("id") == component:
            matches.append(value)
        elif component == "dxvk-mali" and (
            "tools/dxvk/build_mali.sh" in value.get("recipe_inputs", {})
            or any(str(item.get("path", "")).startswith("dxvk-mali/") for item in value.get("files", []))
        ):
            matches.append(value)
    if len(matches) != 1:
        raise ValueError(f"selected source has no unique {component} mapping in tools/powervr/release.json")
    template = matches[0]
    if not isinstance(template.get("files"), list) or not isinstance(template.get("ignored"), list):
        raise ValueError(f"selected source has an invalid {component} mapping")
    return template


def _fallback_template(source: Path, component: str) -> dict:
    """Describe the two long-standing dev artifacts before release.json existed."""
    if component == "dxvk":
        files = [
            {"name": f"{architecture}/{dll}", "path": f"dxvk/{architecture}/{dll}"}
            for architecture in ("x32", "x64") for dll in DLL_NAMES
        ]
        files.extend([
            {"name": "LICENSE", "path": "licenses/dxvk-LICENSE"},
            {"name": "source-commit", "path": "provenance/dxvk-source-commit"},
            {"name": "SHA256SUMS", "path": "provenance/dxvk-SHA256SUMS"},
            {"name": "downstream.patch", "path": "licenses/dxvk-downstream.patch"},
            {"name": "source.tar.zst", "path": "licenses/dxvk-source.tar.zst"},
            {"name": "submodules.txt", "path": "provenance/dxvk-submodules.txt"},
        ])
        ignored = [f"{architecture}/{dll}.a" for architecture in ("x32", "x64") for dll in DLL_NAMES]
        recipe = _required_recipe_paths(source, component)
    elif component == "wsi":
        files = [
            {"name": "libVkLayer_window_system_integration.so", "path": "wsi/libVkLayer_window_system_integration.so"},
            {"name": "VkLayer_window_system_integration.json", "path": "wsi/VkLayer_window_system_integration.json"},
            {"name": "LICENSE", "path": "licenses/wsi-LICENSE"},
            {"name": "source-commit", "path": "provenance/wsi-source-commit"},
            {"name": "SHA256SUMS", "path": "provenance/wsi-SHA256SUMS"},
            {"name": "downstream.patch", "path": "licenses/wsi-downstream.patch"},
            {"name": "source.tar.zst", "path": "licenses/wsi-source.tar.zst"},
            {"name": "vulkan-headers-commit", "path": "provenance/wsi-vulkan-headers-commit"},
        ]
        ignored = []
        recipe = _required_recipe_paths(source, component)
    else:
        raise ValueError(
            f"selected source does not support {component} release mappings; "
            "tools/powervr/release.json is required"
        )
    for relative in recipe:
        if not (source / relative).is_file():
            raise ValueError(f"selected source is missing {component} recipe input: {relative}")
    return {
        "id": component,
        "files": files,
        "ignored": ignored,
        "recipe_inputs": {relative: "" for relative in recipe},
    }


def _required_recipe_paths(source: Path, component: str) -> list[str]:
    fixed = {
        "hybris": (
            "tools/hybris/build_runtime.sh", "tools/hybris/component_record.py",
            "tools/hybris/source.env", "tools/hybris/powervr-hal-version.patch",
        ),
        "dxvk": (
            "tools/dxvk/build.sh", "tools/dxvk/check_present_sync.py", "tools/dxvk/source.env",
        ),
        "wsi": ("tools/vulkan-wsi/build.sh", "tools/vulkan-wsi/source.env"),
        "dxvk-xclipse": (
            "tools/dxvk/build_xclipse.sh", "tools/dxvk/xclipse_component_record.py",
            "tools/dxvk/xclipse-source.env", "tools/tests/dxvk_xclipse_toolchain_test.cpp",
        ),
        "dxvk-mali": (
            "tools/dxvk/build_mali.sh", "tools/dxvk/source.env", "tools/dxvk/mali-source.env",
        ),
    }.get(component)
    if fixed is None:
        raise ValueError(f"no PowerVR recipe contract for component: {component}")
    paths = set(fixed)
    patch_roots = {
        "hybris": ("tools/hybris/patches",),
        "dxvk": ("tools/dxvk/patches",),
        "wsi": ("tools/vulkan-wsi/patches",),
        "dxvk-xclipse": ("tools/dxvk/xclipse-patches",),
        "dxvk-mali": ("tools/dxvk/patches", "tools/dxvk/mali-patches"),
    }[component]
    for root in patch_roots:
        paths.update(str(path.relative_to(source)) for path in (source / root).glob("*.patch"))
    if component == "wsi":
        paths.update(
            str(path.relative_to(source)) for path in (source / "tools/vulkan-wsi/licenses").rglob("*")
            if path.is_file()
        )
    return sorted(paths)


def _recipe_inputs(source: Path, template: dict, component: str) -> dict[str, str]:
    paths = template.get("recipe_inputs")
    if not isinstance(paths, dict) or not paths:
        raise ValueError(f"selected source has no {component} recipe input mapping")
    expected_paths = set(_required_recipe_paths(source, component))
    mapped_paths = set(paths)
    if mapped_paths != expected_paths:
        raise ValueError(
            f"selected source {component} recipe mapping differs from the trusted contract: "
            f"missing={sorted(expected_paths - mapped_paths)}, stale={sorted(mapped_paths - expected_paths)}"
        )
    result = {}
    for relative in sorted(paths):
        if _safe_name(relative) != relative:
            raise ValueError(f"unsafe recipe input path: {relative}")
        path = source / relative
        if not path.is_file():
            raise ValueError(f"selected source is missing mapped {component} recipe input: {relative}")
        result[relative] = sha256(path.read_bytes())

    return result


def _gamescope_recipe(source: Path) -> dict[str, str]:
    paths = [source / "tools/gamescope/build-in-arch.sh", source / "tools/gamescope/runtime-sonames.txt"]
    paths.extend(sorted((source / "tools/gamescope/patches").glob("*.patch")))
    paths.extend(sorted(path for path in (source / "tools/gamescope/tests").rglob("*") if path.is_file()))
    missing = [str(path.relative_to(source)) for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"selected source is missing gamescope recipe inputs: {missing}")
    return {str(path.relative_to(source)): sha256(path.read_bytes()) for path in paths}


def _elf_machine(data: bytes, name: str) -> int:
    if len(data) < 20 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"component file is not ELF64 little-endian: {name}")
    return struct.unpack_from("<H", data, 18)[0]


def _pe_identity(data: bytes, name: str) -> tuple[int, int, set[str]]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError(f"component DLL is not PE: {name}")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe < 0x40 or pe + 26 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"component DLL has no PE header: {name}")
    machine, sections = struct.unpack_from("<HH", data, pe + 4)
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if optional + optional_size > len(data) or optional_size < 2:
        raise ValueError(f"component DLL has a truncated PE optional header: {name}")
    magic = struct.unpack_from("<H", data, optional)[0]
    section_table = optional + optional_size
    if section_table + sections * 40 > len(data):
        raise ValueError(f"component DLL has a truncated PE section table: {name}")
    names = {
        data[section_table + index * 40:section_table + index * 40 + 8]
        .split(b"\0", 1)[0].decode("ascii", "replace")
        for index in range(sections)
    }
    return machine, magic, names


def _validate_pe(component: str, entries, files: list[dict]) -> None:
    dll_names = {item["name"] for item in files if item["name"].lower().endswith(".dll")}
    if component == "dxvk":
        required = {f"{architecture}/{dll}" for architecture in ("x32", "x64") for dll in DLL_NAMES}
    elif component == "dxvk-xclipse":
        prefixes = {name.rsplit("/", 2)[0] for name in dll_names}
        if len(prefixes) != 1:
            raise ValueError("Xclipse DLL inventory has no unique release directory")
        prefix = prefixes.pop()
        required = {f"{prefix}/{architecture}/{dll}" for architecture in ("x32", "x64") for dll in DLL_NAMES}
    else:
        required = {
            f"dxvk-mali/{architecture}/{dll}"
            for architecture in ("i386-windows", "x86_64-windows", "aarch64-windows")
            for dll in DLL_NAMES
        }
    if dll_names != required:
        raise ValueError(
            f"{component} DLL inventory differs: missing={sorted(required - dll_names)}, "
            f"extra={sorted(dll_names - required)}"
        )
    for item in files:
        name = item["name"]
        if not name.lower().endswith(".dll"):
            continue
        content = _read_entry(entries, name)
        machine, magic, sections = _pe_identity(content, name)
        if component in ("dxvk", "dxvk-xclipse"):
            match = re.search(r"/(x32|x64)/[^/]+$", "/" + name)
            if not match:
                raise ValueError(f"component DLL has no recognized architecture directory: {name}")
            expected = {"x32": (0x014C, 0x10B), "x64": (0x8664, 0x20B)}[match.group(1)]
        else:
            match = re.search(r"/(i386-windows|x86_64-windows|aarch64-windows)/[^/]+$", "/" + name)
            if not match:
                raise ValueError(f"component DLL has no recognized architecture directory: {name}")
            expected = {
                "i386-windows": (0x014C, 0x10B),
                "x86_64-windows": (0x8664, 0x20B),
                "aarch64-windows": (0xAA64, 0x20B),
            }[match.group(1)]
            if match.group(1) == "aarch64-windows" and not {".hexpthk", ".a64xrm"}.issubset(sections):
                raise ValueError(f"component ARM64 DLL lacks ARM64X sections: {name}")
        if (machine, magic) != expected:
            raise ValueError(f"component DLL has wrong PE architecture: {name}")


def _checksum_inventory(content: bytes, description: str) -> dict[str, str]:
    result = {}
    for line in content.decode().splitlines():
        digest, separator, name = line.partition("  ")
        name = name.removeprefix("./")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or name in result:
            raise ValueError(f"invalid {description} checksum inventory")
        result[name] = digest
    if not result:
        raise ValueError(f"empty {description} checksum inventory")
    return result


def _validate_hybris(source: Path, entries, recipe: dict) -> None:
    values = _env(source / "tools/hybris/source.env")
    if _read_entry(entries, "source-commit").decode().strip() != values.get("HYBRIS_REVISION"):
        raise ValueError("hybris archive was built from a different source revision")
    if json.loads(_read_entry(entries, "build-recipe.json")) != recipe:
        raise ValueError("hybris archive was built with a different recipe or patch stack")
    for name in REQUIRED_HYBRIS_LIBRARIES:
        if _elf_machine(_read_entry(entries, name), name) != 183:
            raise ValueError(f"hybris library is not ELF64 AArch64: {name}")
    manifest = _object(json.loads(_read_entry(entries, "build-manifest.json")), "hybris build manifest")
    if manifest.get("source_commit") != values.get("HYBRIS_REVISION") or not manifest.get("compiler"):
        raise ValueError("hybris build manifest lacks matching source/compiler provenance")
    checksums = _checksum_inventory(_read_entry(entries, "SHA256SUMS"), "hybris")
    actual = {
        name: sha256(content) for name, (kind, _, content) in entries.items()
        if name.startswith("lib/") and kind == "file"
    }
    if checksums != actual:
        raise ValueError("hybris packaged library checksum inventory differs")
    for name in ("licenses/powervr.patch", "licenses/libhybris-source.tar.zst", "licenses/wayland-android.xml"):
        if not _read_entry(entries, name):
            raise ValueError(f"empty hybris source/license file: {name}")
    placed = _object(json.loads(_read_entry(entries, "lib/VkLayer_DROIDDECK_placed_memory.json")), "placed-memory manifest")
    layer = _object(placed.get("layer"), "placed-memory layer")
    if (layer.get("name") != "VK_LAYER_DROIDDECK_placed_memory"
            or layer.get("library_path") != "./libVkLayer_DROIDDECK_placed_memory.so"):
        raise ValueError("invalid placed-memory layer manifest")


def _validate_dxvk(source: Path, entries, component: str, recipe: dict) -> None:
    if component == "dxvk":
        values = _env(source / "tools/dxvk/source.env")
        revision = values.get("DXVK_REVISION")
        source_name = "source-commit"
        sums_name = "SHA256SUMS"
    elif component == "dxvk-xclipse":
        values = _env(source / "tools/dxvk/xclipse-source.env")
        revision = values.get("DXVK_XCLIPSE_REVISION")
        version = values.get("DXVK_XCLIPSE_VERSION")
        prefix = f"dxvk-{version}"
        source_name = f"{prefix}/source-commit"
        sums_name = f"{prefix}/provenance/SHA256SUMS"
        if json.loads(_read_entry(entries, f"{prefix}/provenance/build-recipe.json")) != recipe:
            raise ValueError("Xclipse archive was built with a different recipe or patch stack")
        manifest = _object(json.loads(_read_entry(entries, f"{prefix}/provenance/build-manifest.json")), "Xclipse build manifest")
        if manifest.get("source_commit") != revision or manifest.get("source_version") != version:
            raise ValueError("Xclipse build manifest source differs")
    else:
        values = _env(source / "tools/dxvk/source.env")
        revision = values.get("DXVK_REVISION")
        source_name = "source-commit"
        sums_name = "SHA256SUMS"
    if _read_entry(entries, source_name).decode().strip() != revision:
        raise ValueError(f"{component} archive was built from a different source revision")
    checksums = _checksum_inventory(_read_entry(entries, sums_name), component)
    prefix = "" if component != "dxvk-xclipse" else source_name.rsplit("/", 1)[0] + "/"
    actual = {
        name: sha256(content) for name, (kind, _, content) in entries.items()
        if kind == "file" and name.lower().endswith(".dll")
    }
    normalized = {prefix + name if prefix and not name.startswith(prefix) else name: digest for name, digest in checksums.items()}
    checksum_dlls = {name: digest for name, digest in normalized.items() if name.lower().endswith(".dll")}
    if checksum_dlls != actual:
        raise ValueError(f"{component} DLL checksum inventory differs")


def _validate_wsi(source: Path, entries) -> None:
    values = _env(source / "tools/vulkan-wsi/source.env")
    expected = {
        "source-commit": values.get("WSI_REVISION"),
        "vulkan-headers-commit": values.get("VULKAN_HEADERS_REVISION"),
    }
    for name, revision in expected.items():
        if _read_entry(entries, name).decode().strip() != revision:
            raise ValueError(f"WSI archive has a different {name}")
    library = "libVkLayer_window_system_integration.so"
    if _elf_machine(_read_entry(entries, library), library) != 183:
        raise ValueError("WSI library is not ELF64 AArch64")
    checksums = _checksum_inventory(_read_entry(entries, "SHA256SUMS"), "WSI")
    if checksums != {library: sha256(_read_entry(entries, library))}:
        raise ValueError("WSI library checksum inventory differs")
    _object(json.loads(_read_entry(entries, "VkLayer_window_system_integration.json")), "WSI layer manifest")


def _validate_mali(source: Path, entries) -> None:
    values = _env(source / "tools/dxvk/source.env")
    if _read_entry(entries, "source-commit").decode().strip() != values.get("DXVK_REVISION"):
        raise ValueError("Mali DXVK archive was built from a different source revision")
    checksums = _checksum_inventory(_read_entry(entries, "SHA256SUMS"), "Mali DXVK")
    actual = {
        name: sha256(content) for name, (kind, _, content) in entries.items()
        if kind == "file" and name != "SHA256SUMS"
    }
    # The producer checksum also covers run-proton.py. The APK already carries
    # its reviewed wrapper separately, so the release mapping intentionally
    # omits that file while retaining the producer's complete inventory.
    if any(checksums.get(name) != digest for name, digest in actual.items()):
        raise ValueError("Mali DXVK checksum inventory differs")


def _pin_from_template(component: str, template: dict, entries, recipe: dict) -> dict:
    expected = {}
    files = []
    for item in template["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str):
            raise ValueError(f"selected source has an invalid {component} file mapping")
        name = _safe_name(item["name"])
        target = _safe_name(item["path"])
        if name in expected:
            raise ValueError(f"selected source has duplicate {component} file mapping: {name}")
        expected[name] = target
    ignored = [_safe_name(name) for name in template["ignored"]]
    if len(ignored) != len(set(ignored)) or set(expected) & set(ignored):
        raise ValueError(f"selected source has overlapping {component} file mappings")
    archive_names = set(entries)
    allowed = set(expected) | set(ignored)
    if archive_names != allowed:
        raise ValueError(
            f"{component} archive member set differs from its source mapping: "
            f"missing={sorted(allowed - archive_names)}, extra={sorted(archive_names - allowed)}"
        )
    destinations = set()
    for name, target in expected.items():
        kind, mode, content = entries[name]
        if kind != "file" or not isinstance(content, bytes):
            raise ValueError(f"mapped component member is not a regular file: {name}")
        if target in destinations:
            raise ValueError(f"duplicate component destination: {target}")
        destinations.add(target)
        files.append({
            "mode": mode,
            "name": name,
            "path": target,
            "sha256": sha256(content),
            "size": len(content),
        })
    pin = {
        "files": files,
        "id": component,
        "ignored": ignored,
        "recipe_inputs": recipe,
    }
    if template.get("format"):
        pin["format"] = template["format"]
    return pin


def _write_zip(path: Path, entries: dict[str, tuple[str, int, bytes | str]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (kind, mode, content) in sorted(entries.items()):
            if kind != "file" or not isinstance(content, bytes):
                raise ValueError(f"cannot normalize non-file ZIP member: {name}")
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def _directory_entries(directory: Path) -> dict[str, tuple[str, int, bytes | str]]:
    entries = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"build output contains a symlink: {path}")
        if not path.is_file():
            continue
        name = str(path.relative_to(directory))
        content = path.read_bytes()
        entries[name] = ("file", 0o755 if path.stat().st_mode & 0o111 else 0o644, content)
    if not entries:
        raise ValueError(f"component build output is empty: {directory}")
    return entries


def _source_identity(source_record: dict) -> tuple[dict, dict]:
    source_record = _object(source_record, "source record")
    repository = source_record.get("repository")
    commit = source_record.get("commit")
    if not isinstance(repository, str) or not repository or not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise ValueError("source record must contain repository and full commit")
    source = {"repository": repository, "commit": commit}
    provenance_keys = (
        "requested_repository", "requested_ref", "publish_repository", "release_tag",
        "controller_repository", "controller_commit", "run_url",
    )
    provenance = {key: source_record.get(key) for key in provenance_keys}
    for key, value in provenance.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"source record must contain {key}")
    if not re.fullmatch(r"[0-9a-f]{40}", provenance["controller_commit"]):
        raise ValueError("source record controller_commit must be a full commit")
    return source, provenance


def create_record(component: str, source: Path, build_dir: Path, output: Path, source_record: dict) -> dict:
    if component not in COMPONENTS:
        raise ValueError(f"unsupported component: {component}")
    source = source.resolve()
    build_dir = build_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f"record output directory must be empty: {output}")
    release = None
    if component != "gamescope":
        release_path = source / "tools/powervr/release.json"
        if release_path.is_file():
            release = _source_release(source)
        elif component not in ("dxvk", "wsi"):
            # Older dev revisions can still produce the established DXVK/WSI
            # artifacts. New component layouts require an explicit source map.
            _fallback_template(source, component)
    asset_name = ASSET_NAMES[component]
    asset_path = output / asset_name

    archive_template = None
    if component not in ("hybris", "gamescope"):
        archive_template = _archive_template(release, component) if release is not None else _fallback_template(source, component)

    if component in ("dxvk", "wsi"):
        entries = _directory_entries(build_dir)
        _write_zip(asset_path, entries)
    elif component == "dxvk-mali":
        combined = build_dir / "dxvk-mali-payloads.tar.zst"
        if not combined.is_file():
            raise ValueError("Mali DXVK build output is missing dxvk-mali-payloads.tar.zst")
        combined_entries = _read_archive(combined.read_bytes(), combined.name)
        prefix = "experimental/dxvk-mali/"
        entries = {
            name.removeprefix(prefix): value
            for name, value in combined_entries.items()
            if name.startswith(prefix)
        }
        if not entries:
            raise ValueError("Mali DXVK build output has no experimental payload")
        assert archive_template is not None
        allowed = {item["name"] for item in archive_template["files"]} | set(archive_template["ignored"])
        missing = allowed - set(entries)
        if missing:
            raise ValueError(f"Mali DXVK build output is missing mapped files: {sorted(missing)}")
        entries = {name: value for name, value in entries.items() if name in allowed}
        _write_zip(asset_path, entries)
    else:
        source_name = {
            "hybris": "runtime.tzst",
            "dxvk-xclipse": next((path.name for path in build_dir.glob("dxvk-*.tar.gz")), ""),
            "gamescope": "gamescope.tzst",
        }[component]
        source_path = build_dir / source_name
        if not source_name or not source_path.is_file():
            raise ValueError(f"{component} build output is missing its component archive")
        shutil.copyfile(source_path, asset_path)
        entries = _read_archive(asset_path.read_bytes(), asset_name)

    if component == "gamescope":
        if set(entries) != {"usr/local/bin/gamescope"}:
            raise ValueError("gamescope archive contains unexpected files")
        game = _read_entry(entries, "usr/local/bin/gamescope")
        if _elf_machine(game, "usr/local/bin/gamescope") != 183:
            raise ValueError("gamescope component is not ELF64 AArch64")
        values = _env(source / "tools/gamescope/release.env")
        recipe = _gamescope_recipe(source)
        pin = {
            "sha256": sha256(asset_path.read_bytes()),
            "size": asset_path.stat().st_size,
        }
        if not values.get("GAMESCOPE_TAG") or not values.get("GAMESCOPE_SHA256"):
            raise ValueError("selected source has an unsupported gamescope release mapping")
    elif component == "hybris":
        assert release is not None
        template = _object(release.get("hybris"), "hybris release mapping")
        recipe = _recipe_inputs(source, template, component)
        _validate_hybris(source, entries, recipe)
        pin = {
            "sha256": sha256(asset_path.read_bytes()),
            "size": asset_path.stat().st_size,
            "tree_sha256": _tree_digest(entries),
            "recipe_inputs": recipe,
        }
    else:
        assert archive_template is not None
        template = archive_template
        recipe = _recipe_inputs(source, template, component)
        if component in ("dxvk", "dxvk-xclipse"):
            _validate_dxvk(source, entries, component, recipe)
        elif component == "wsi":
            _validate_wsi(source, entries)
        else:
            _validate_mali(source, entries)
        pin = _pin_from_template(component, template, entries, recipe)
        if component in ("dxvk", "dxvk-xclipse", "dxvk-mali"):
            _validate_pe(component, entries, pin["files"])

    if component not in ("hybris", "gamescope"):
        pin["sha256"] = sha256(asset_path.read_bytes())
        pin["size"] = asset_path.stat().st_size

    identity, provenance = _source_identity(source_record)
    data = asset_path.read_bytes()
    record = {
        "format": FORMAT,
        "component": component,
        "source": identity,
        "artifact": {"name": asset_name, "size": len(data), "sha256": sha256(data)},
        "recipe_inputs": recipe,
        "pin": pin,
        "provenance": provenance,
    }
    record_path = output / f"{component}-component.json"
    record_path.write_bytes(json_bytes(record))
    verify_record(output, record, expected_source=identity)
    return record


def _expected_source(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike, Path)):
        value = json.loads(Path(value).read_text())
    value = _object(value, "expected source")
    if isinstance(value.get("source"), dict):
        value = value["source"]
    result = {"repository": value.get("repository"), "commit": value.get("commit")}
    if not isinstance(result["repository"], str) or not re.fullmatch(r"[0-9a-f]{40}", str(result["commit"])):
        raise ValueError("expected source must contain repository and full commit")
    return result


def verify_record(directory: Path | str, record, expected_source=None) -> None:
    directory = Path(directory)
    if isinstance(record, (str, os.PathLike, Path)):
        record = json.loads(Path(record).read_text())
    record = _object(record, "component record")
    required = {"format", "component", "source", "artifact", "recipe_inputs", "pin", "provenance"}
    if set(record) != required or record.get("format") != FORMAT or record.get("component") not in COMPONENTS:
        raise ValueError("invalid component record schema")
    component = record["component"]
    source = _object(record["source"], "component source")
    if set(source) != {"repository", "commit"} or not isinstance(source["repository"], str) or not source["repository"]:
        raise ValueError("invalid component source identity")
    if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit"))):
        raise ValueError("component source commit must be a full commit")
    expected = _expected_source(expected_source)
    if expected is not None and source != expected:
        raise ValueError(f"component source differs: expected={expected}, actual={source}")
    provenance = _object(record["provenance"], "component provenance")
    provenance_keys = {
        "requested_repository", "requested_ref", "publish_repository", "release_tag",
        "controller_repository", "controller_commit", "run_url",
    }
    if set(provenance) != provenance_keys or any(not isinstance(provenance[key], str) or not provenance[key] for key in provenance_keys):
        raise ValueError("invalid component provenance")
    if not re.fullmatch(r"[0-9a-f]{40}", provenance["controller_commit"]):
        raise ValueError("component controller commit must be a full commit")
    recipe = _object(record["recipe_inputs"], "component recipe inputs")
    if not recipe or any(_safe_name(name) != name or not re.fullmatch(r"[0-9a-f]{64}", str(digest)) for name, digest in recipe.items()):
        raise ValueError("invalid component recipe inputs")

    artifact = _object(record["artifact"], "component artifact")
    if set(artifact) != {"name", "size", "sha256"} or artifact.get("name") != ASSET_NAMES[component]:
        raise ValueError("invalid component artifact identity")
    path = directory / artifact["name"]
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"component artifact is missing: {path}")
    if not isinstance(artifact.get("size"), int) or artifact["size"] <= 0 or artifact["size"] > MAX_ARCHIVE_BYTES:
        raise ValueError("invalid component artifact size")
    data = path.read_bytes()
    if len(data) != artifact["size"] or sha256(data) != artifact.get("sha256"):
        raise ValueError("component artifact size or checksum differs")
    entries = _read_archive(data, artifact["name"])

    pin = _object(record["pin"], "component pin")
    if component == "gamescope":
        required_pin = {"sha256", "size"}
        if set(pin) != required_pin or pin["sha256"] != artifact["sha256"] or pin["size"] != artifact["size"]:
            raise ValueError("gamescope pin differs from its artifact")
        if set(entries) != {"usr/local/bin/gamescope"}:
            raise ValueError("gamescope archive contains unexpected files")
        if _elf_machine(_read_entry(entries, "usr/local/bin/gamescope"), "usr/local/bin/gamescope") != 183:
            raise ValueError("gamescope component is not ELF64 AArch64")
        return
    if pin.get("recipe_inputs") != recipe:
        raise ValueError("component pin recipe differs from its record")
    if component == "hybris":
        if set(pin) != {"sha256", "size", "tree_sha256", "recipe_inputs"}:
            raise ValueError("invalid hybris pin schema")
        if pin["sha256"] != artifact["sha256"] or pin["size"] != artifact["size"]:
            raise ValueError("hybris pin differs from its artifact")
        if pin.get("tree_sha256") != _tree_digest(entries):
            raise ValueError("hybris archive tree checksum differs")
        for name in REQUIRED_HYBRIS_LIBRARIES:
            if _elf_machine(_read_entry(entries, name), name) != 183:
                raise ValueError(f"hybris library is not ELF64 AArch64: {name}")
        return

    allowed_pin = {"files", "id", "ignored", "recipe_inputs", "format", "sha256", "size"}
    required_pin = allowed_pin - {"format"}
    if not set(pin).issubset(allowed_pin) or set(pin) - {"format"} != required_pin or pin.get("id") != component:
        raise ValueError("invalid archive component pin schema")
    if pin["sha256"] != artifact["sha256"] or pin["size"] != artifact["size"]:
        raise ValueError("archive component pin differs from its artifact")
    files = pin.get("files")
    ignored = pin.get("ignored")
    if not isinstance(files, list) or not isinstance(ignored, list):
        raise ValueError("invalid archive component inventory")
    expected_names, destinations = set(), set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"mode", "name", "path", "sha256", "size"}:
            raise ValueError("invalid component file pin")
        name, target = _safe_name(item["name"]), _safe_name(item["path"])
        if name in expected_names or target in destinations:
            raise ValueError("duplicate component file pin")
        expected_names.add(name)
        destinations.add(target)
        kind, mode, content = entries.get(name, (None, None, None))
        if (kind != "file" or mode != item["mode"] or not isinstance(content, bytes)
                or len(content) != item["size"] or sha256(content) != item["sha256"]):
            raise ValueError(f"component member differs from its pin: {name}")
    ignored_names = {_safe_name(name) for name in ignored}
    if len(ignored_names) != len(ignored) or ignored_names & expected_names or set(entries) != expected_names | ignored_names:
        raise ValueError("component archive member set differs from its pin")
    if component in ("dxvk", "dxvk-xclipse", "dxvk-mali"):
        _validate_pe(component, entries, files)


def verify_source(directory: Path | str, record, source: Path | str) -> None:
    """Bind a verified record to a fresh, clean checkout of its selected source."""
    directory = Path(directory)
    source = Path(source).resolve()
    if isinstance(record, (str, os.PathLike, Path)):
        record = json.loads(Path(record).read_text())
    record = _object(record, "component record")
    verify_record(directory, record)
    try:
        top = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        head = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], text=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("selected source is not a readable Git checkout") from error
    if Path(top).resolve() != source:
        raise ValueError("selected source path is not the Git checkout root")
    if head != record.get("source", {}).get("commit"):
        raise ValueError("selected source checkout differs from the recorded commit")
    if dirty:
        raise ValueError("selected source checkout is dirty")

    component = record["component"]
    artifact = record["artifact"]
    data = (directory / artifact["name"]).read_bytes()
    entries = _read_archive(data, artifact["name"])
    if component == "gamescope":
        recipe = _gamescope_recipe(source)
        if set(entries) != {"usr/local/bin/gamescope"}:
            raise ValueError("gamescope archive contains unexpected files")
        kind, mode, game = entries["usr/local/bin/gamescope"]
        if kind != "file" or mode != 0o755 or not isinstance(game, bytes) or _elf_machine(game, "usr/local/bin/gamescope") != 183:
            raise ValueError("gamescope component is not executable ELF64 AArch64")
        expected_pin = {"sha256": artifact["sha256"], "size": artifact["size"]}
    else:
        release_path = source / "tools/powervr/release.json"
        release = _source_release(source) if release_path.is_file() else None
        if component == "hybris":
            if release is None:
                raise ValueError("selected source does not provide a hybris release mapping")
            template = _object(release.get("hybris"), "hybris release mapping")
            recipe = _recipe_inputs(source, template, component)
            _validate_hybris(source, entries, recipe)
            expected_pin = {
                "sha256": artifact["sha256"],
                "size": artifact["size"],
                "tree_sha256": _tree_digest(entries),
                "recipe_inputs": recipe,
            }
        else:
            template = _archive_template(release, component) if release is not None else _fallback_template(source, component)
            recipe = _recipe_inputs(source, template, component)
            if component in ("dxvk", "dxvk-xclipse"):
                _validate_dxvk(source, entries, component, recipe)
            elif component == "wsi":
                _validate_wsi(source, entries)
            else:
                _validate_mali(source, entries)
            expected_pin = _pin_from_template(component, template, entries, recipe)
            expected_pin.update(sha256=artifact["sha256"], size=artifact["size"])
            if component in ("dxvk", "dxvk-xclipse", "dxvk-mali"):
                _validate_pe(component, entries, expected_pin["files"])
    if record["recipe_inputs"] != recipe:
        raise ValueError("recorded recipe inputs differ from the clean selected source")
    if record["pin"] != expected_pin:
        raise ValueError("recorded component pin differs from the clean selected source")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True, choices=COMPONENTS)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-record", required=True, type=Path)
    args = parser.parse_args()
    record = create_record(
        args.component, args.source, args.build_dir, args.output,
        json.loads(args.source_record.read_text()),
    )
    print(json.dumps({"component": args.component, "artifact": record["artifact"]}, sort_keys=True))


if __name__ == "__main__":
    main()

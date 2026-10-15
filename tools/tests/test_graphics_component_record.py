import copy
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import tempfile
import unittest
import warnings
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "graphics_component_record",
    Path(__file__).resolve().parents[1] / "graphics-ci/record.py",
)
record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(record)


def pe(machine, magic, sections=()):
    offset = 0x80
    optional_size = 0xF0
    data = bytearray(offset + 24 + optional_size + 40 * len(sections))
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, offset)
    data[offset:offset + 4] = b"PE\0\0"
    struct.pack_into("<HH", data, offset + 4, machine, len(sections))
    struct.pack_into("<H", data, offset + 20, optional_size)
    struct.pack_into("<H", data, offset + 24, magic)
    section_table = offset + 24 + optional_size
    for index, name in enumerate(sections):
        encoded = name.encode()
        data[section_table + index * 40:section_table + index * 40 + len(encoded)] = encoded
    return bytes(data)


class GraphicsComponentRecordTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.build = self.root / "build"
        self.output = self.root / "output"
        self.build.mkdir(parents=True)
        dxvk = self.source / "tools/dxvk"
        (dxvk / "patches").mkdir(parents=True)
        (dxvk / "build.sh").write_text("build recipe\n")
        (dxvk / "check_present_sync.py").write_text("validation recipe\n")
        (dxvk / "source.env").write_text("DXVK_REVISION=" + "a" * 40 + "\n")
        (dxvk / "patches/0001-test.patch").write_text("patch\n")
        self.source_record = {
            "repository": "example/source",
            "commit": "b" * 40,
            "requested_repository": "example/source",
            "requested_ref": "feature",
            "publish_repository": "example/components",
            "release_tag": "graphics-test",
            "controller_repository": "example/controller",
            "controller_commit": "c" * 40,
            "run_url": "run:123",
        }

    def populate_dxvk(self):
        sums = []
        for architecture, identity in {"x32": (0x014C, 0x10B), "x64": (0x8664, 0x20B)}.items():
            directory = self.build / architecture
            directory.mkdir()
            for dll in record.DLL_NAMES:
                content = pe(*identity)
                path = directory / dll
                path.write_bytes(content)
                path.chmod(0o755)
                (directory / (dll + ".a")).write_bytes(b"import library")
                sums.append(f"{record.sha256(content)}  {architecture}/{dll}\n")
        metadata = {
            "LICENSE": b"license",
            "source-commit": ("a" * 40 + "\n").encode(),
            "SHA256SUMS": "".join(sums).encode(),
            "downstream.patch": b"patch",
            "source.tar.zst": b"source archive",
            "submodules.txt": b"submodule inventory",
        }
        for name, content in metadata.items():
            (self.build / name).write_bytes(content)

    def create_dxvk(self):
        self.populate_dxvk()
        return record.create_record(
            "dxvk", self.source, self.build, self.output, self.source_record,
        )

    def populate_wsi(self, build, include_licenses, tracked_licenses=True):
        wsi = self.source / "tools/vulkan-wsi"
        (wsi / "patches").mkdir(parents=True, exist_ok=True)
        (wsi / "build.sh").write_text("WSI build recipe\n")
        (wsi / "source.env").write_text(
            "WSI_REVISION=" + "d" * 40 + "\n"
            "VULKAN_HEADERS_REVISION=" + "e" * 40 + "\n"
        )
        (wsi / "patches/0001-test.patch").write_text("WSI patch\n")
        if tracked_licenses:
            licenses = wsi / "licenses/Vulkan-Headers"
            licenses.mkdir(parents=True, exist_ok=True)
            (licenses / "LICENSE.md").write_text("Vulkan-Headers license\n")
            (licenses / "MIT.txt").write_text("MIT license\n")
        build.mkdir(parents=True)
        elf = bytearray(64)
        elf[:6], elf[18:20] = b"\x7fELF\x02\x01", struct.pack("<H", 183)
        library = bytes(elf)
        files = {
            "libVkLayer_window_system_integration.so": library,
            "VkLayer_window_system_integration.json": b"{}\n",
            "LICENSE": b"WSI license\n",
            "source-commit": ("d" * 40 + "\n").encode(),
            "SHA256SUMS": (record.sha256(library) + "  libVkLayer_window_system_integration.so\n").encode(),
            "downstream.patch": b"WSI patch\n",
            "source.tar.zst": b"source archive",
            "vulkan-headers-commit": ("e" * 40 + "\n").encode(),
        }
        if include_licenses:
            for name, source_name in record.WSI_PROVENANCE_LICENSES.items():
                tracked = self.source / source_name
                files[name] = tracked.read_bytes() if tracked.is_file() else b"untracked license\n"
        for name, content in files.items():
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_records_dev_dxvk_without_release_template_and_recomputes_pin(self):
        result = self.create_dxvk()
        self.assertEqual(result["artifact"]["name"], "dxvk.zip")
        self.assertEqual(result["pin"]["sha256"], result["artifact"]["sha256"])
        self.assertEqual(result["pin"]["size"], result["artifact"]["size"])
        self.assertNotIn("url", result["pin"])
        self.assertNotIn("provenance", result["pin"])
        self.assertEqual(len(result["pin"]["files"]), 16)
        self.assertIn("tools/dxvk/patches/0001-test.patch", result["recipe_inputs"])
        record.verify_record(self.output, result, {
            "repository": "example/source", "commit": "b" * 40,
        })

    def test_verify_rejects_changed_archive_hash_and_source(self):
        result = self.create_dxvk()
        archive = self.output / "dxvk.zip"
        archive.write_bytes(archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "size or checksum"):
            record.verify_record(self.output, result)
        archive.write_bytes(archive.read_bytes()[:-7])
        with self.assertRaisesRegex(ValueError, "component source differs"):
            record.verify_record(self.output, result, {
                "repository": "other/source", "commit": "b" * 40,
            })

    def test_wsi_accepts_old_and_new_license_layouts_without_destinations(self):
        records = {}
        for include_licenses in (False, True):
            build = self.root / f"wsi-build-{include_licenses}"
            output = self.root / f"wsi-output-{include_licenses}"
            self.populate_wsi(build, include_licenses)
            result = record.create_record(
                "wsi", self.source, build, output, self.source_record,
            )
            record.verify_record(output, result)
            self.assertEqual(
                result["pin"]["ignored"], sorted(record.WSI_PROVENANCE_LICENSES),
            )
            self.assertFalse(
                set(record.WSI_PROVENANCE_LICENSES)
                & {item["name"] for item in result["pin"]["files"]}
            )
            entries = record._read_zip((output / "wsi.zip").read_bytes())
            self.assertEqual(
                set(entries) & set(record.WSI_PROVENANCE_LICENSES),
                set(record.WSI_PROVENANCE_LICENSES) if include_licenses else set(),
            )
            records[include_licenses] = (output, result)

        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.source), "-c", "user.name=Graphics Test",
            "-c", "user.email=graphics-test@example.invalid", "commit", "-qm", "fixture",
        ], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True,
        ).strip()
        output, result = records[True]
        result["source"]["commit"] = head
        record.verify_source(output, result, self.source)

    def test_rejects_wsi_license_that_differs_from_selected_source(self):
        build = self.root / "wsi-build"
        self.populate_wsi(build, True)
        (build / "licenses/Vulkan-Headers/MIT.txt").write_text("substituted license\n")
        with self.assertRaisesRegex(ValueError, "license differs"):
            record.create_record("wsi", self.source, build, self.output, self.source_record)

    def test_legacy_wsi_source_without_tracked_licenses_remains_supported(self):
        build = self.root / "legacy-wsi-build"
        self.populate_wsi(build, False, tracked_licenses=False)
        result = record.create_record("wsi", self.source, build, self.output, self.source_record)
        self.assertEqual(result["pin"]["ignored"], [])
        record.verify_record(self.output, result)

        extra_build = self.root / "legacy-wsi-extra-build"
        self.populate_wsi(extra_build, True, tracked_licenses=False)
        extra_output = self.root / "legacy-wsi-extra-output"
        with self.assertRaisesRegex(ValueError, "no tracked selected-source copy"):
            record.create_record("wsi", self.source, extra_build, extra_output, self.source_record)

    def test_rejects_source_mapping_that_omits_a_built_member(self):
        self.populate_dxvk()
        fallback = record._fallback_template(self.source, "dxvk")
        fallback["format"] = 1
        fallback["url"] = "https://fork.invalid/old.zip"
        fallback["provenance"] = {"ci_run": "https://fork.invalid/run"}
        fallback["sha256"] = "0" * 64
        fallback["size"] = 1
        fallback["files"] = fallback["files"][:-1]
        release = {"format": 1, "archives": [fallback], "hybris": {}}
        path = self.source / "tools/powervr/release.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(release))
        with self.assertRaisesRegex(ValueError, "member set differs"):
            record.create_record("dxvk", self.source, self.build, self.output, self.source_record)

    def test_rejects_source_mapping_that_omits_a_required_recipe(self):
        self.populate_dxvk()
        fallback = record._fallback_template(self.source, "dxvk")
        del fallback["recipe_inputs"]["tools/dxvk/check_present_sync.py"]
        release = {"format": 1, "archives": [fallback], "hybris": {}}
        path = self.source / "tools/powervr/release.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(release))
        with self.assertRaisesRegex(ValueError, "trusted contract"):
            record.create_record("dxvk", self.source, self.build, self.output, self.source_record)

    def test_rejects_traversal_duplicate_and_symlink_zip_members(self):
        cases = []
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../escape", b"bad")
        cases.append((stream.getvalue(), "unsafe archive path"))

        stream = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("same", b"one")
                archive.writestr("same", b"two")
        cases.append((stream.getvalue(), "duplicate archive path"))

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        cases.append((stream.getvalue(), "unsupported archive entry type"))

        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                record._read_zip(data)

    def test_rejects_hardlinks_in_tar(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            target = tarfile.TarInfo("target")
            target.size = 4
            archive.addfile(target, io.BytesIO(b"data"))
            link = tarfile.TarInfo("hardlink")
            link.type = tarfile.LNKTYPE
            link.linkname = "target"
            archive.addfile(link)
        with self.assertRaisesRegex(ValueError, "unsafe hardlink"):
            record._read_tar(stream.getvalue(), "gzip")

    def test_rejects_wrong_pe_architecture_and_non_arm64x_mali(self):
        files = [{
            "name": f"dxvk-mali/{architecture}/{dll}",
            "path": f"dxvk-mali/{architecture}/{dll}",
        } for architecture in ("i386-windows", "x86_64-windows", "aarch64-windows")
            for dll in record.DLL_NAMES]
        identities = {
            "i386-windows": (0x014C, 0x10B),
            "x86_64-windows": (0x8664, 0x20B),
            "aarch64-windows": (0xAA64, 0x20B),
        }
        entries = {}
        for item in files:
            architecture = item["name"].split("/")[1]
            entries[item["name"]] = ("file", 0o755, pe(*identities[architecture]))
        with self.assertRaisesRegex(ValueError, "lacks ARM64X sections"):
            record._validate_pe("dxvk-mali", entries, files)
        name = "dxvk-mali/aarch64-windows/d3d8.dll"
        entries[name] = ("file", 0o755, pe(0x8664, 0x20B, (".hexpthk", ".a64xrm")))
        with self.assertRaisesRegex(ValueError, "wrong PE architecture"):
            record._validate_pe("dxvk-mali", entries, files)

    def test_verify_rejects_forged_pin_inventory(self):
        result = self.create_dxvk()
        forged = copy.deepcopy(result)
        forged["pin"]["files"][0]["path"] = "../escape"
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            record.verify_record(self.output, forged)

    def test_verify_source_binds_record_to_clean_exact_checkout(self):
        self.populate_dxvk()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.source), "-c", "user.name=Graphics Test",
            "-c", "user.email=graphics-test@example.invalid", "commit", "-qm", "fixture",
        ], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.source_record["commit"] = head
        result = record.create_record(
            "dxvk", self.source, self.build, self.output, self.source_record,
        )
        record.verify_source(self.output, result, self.source)

        forged = copy.deepcopy(result)
        forged["recipe_inputs"]["tools/dxvk/build.sh"] = "0" * 64
        forged["pin"]["recipe_inputs"]["tools/dxvk/build.sh"] = "0" * 64
        record.verify_record(self.output, forged)
        with self.assertRaisesRegex(ValueError, "clean selected source"):
            record.verify_source(self.output, forged, self.source)

        (self.source / "tools/dxvk/build.sh").write_text("changed after recording\n")
        with self.assertRaisesRegex(ValueError, "checkout is dirty"):
            record.verify_source(self.output, result, self.source)


if __name__ == "__main__":
    unittest.main()

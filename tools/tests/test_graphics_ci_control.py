import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("graphics_ci_control", Path(__file__).parents[1] / "graphics-ci/control.py")
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
COMMIT = "a" * 40
CONTROLLER = "b" * 40


class SourceResolutionTests(unittest.TestCase):
    def test_pr_uses_head_repository_and_commit(self):
        result = {"head": {"repo": {"full_name": "contributor/project"}, "sha": COMMIT}}
        with patch.object(control, "api", return_value=result) as request:
            self.assertEqual(control.resolve_source("upstream/project", "pr:570"), ("contributor/project", COMMIT))
            request.assert_called_once_with("repos/upstream/project/pulls/570")

    def test_pr_url_selects_its_repository(self):
        result = {"head": {"repo": {"full_name": "contributor/project"}, "sha": COMMIT}}
        with patch.object(control, "api", return_value=result) as request:
            control.resolve_source("controller/project", "https://github.com/upstream/project/pull/570")
            request.assert_called_once_with("repos/upstream/project/pulls/570")

    def test_branch_slashes_are_encoded(self):
        with patch.object(control, "api", return_value={"sha": COMMIT}) as request:
            self.assertEqual(control.resolve_source("owner/repo", "work/graphics"), ("owner/repo", COMMIT))
            request.assert_called_once_with("repos/owner/repo/commits/work%2Fgraphics")

    def test_malformed_inputs_never_reach_api(self):
        with patch.object(control, "api") as request:
            for repo, ref in [("../repo", "dev"), ("a/b/c", "dev"), ("a/b", "https://evil.example/a/b/pull/1"),
                              ("a/b", "https://github.com/a/b/pull/1?x=y"), ("a/b", "dev\nother=value"),
                              ("a/b", "file:///tmp/ref")]:
                with self.subTest(repo=repo, ref=ref), self.assertRaises(ValueError):
                    control.resolve_source(repo, ref)
            request.assert_not_called()

    def test_deleted_head_and_short_sha_rejected(self):
        for result in ({"head": {"repo": None, "sha": COMMIT}}, {"head": {"repo": {"full_name": "a/b"}, "sha": "abc"}}):
            with patch.object(control, "api", return_value=result), self.assertRaises(ValueError):
                control.resolve_source("a/b", "pr:1")

    def test_unsafe_tags_rejected(self):
        for tag in ("-option", "line\nbreak", "a/../b", "a..b", "$(command)"):
            with self.subTest(tag=tag), self.assertRaises((ValueError, control.subprocess.CalledProcessError)):
                control.tag_name(tag)

    def test_matrix_uses_native_arm_for_wsi_and_gamescope(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"
            env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": CONTROLLER,
                   "GITHUB_RUN_ID": "123", "GITHUB_OUTPUT": str(out), "COMPONENTS": "all"}
            with patch.object(control, "resolve_source", return_value=("fork/repo", COMMIT)):
                control.resolve(Path(temp) / "source.json", env)
            values = dict(line.split("=", 1) for line in out.read_text().splitlines())
            matrix = {row["component"]: row["runner"] for row in json.loads(values["matrix"])["include"]}
            self.assertEqual(matrix["wsi"], "ubuntu-24.04-arm")
            self.assertEqual(matrix["gamescope"], "ubuntu-24.04-arm")
            self.assertEqual(matrix["hybris"], "ubuntu-24.04")


class PublicationBoundaryTests(unittest.TestCase):
    def source(self):
        return dict(format=1, repository="source/repo", commit=COMMIT, components=["dxvk"],
                    requested_repository="source/repo", requested_ref="dev",
                    publish_repository="owner/repo", release_tag="graphics-test",
                    controller_repository="owner/repo", controller_commit=CONTROLLER,
                    run_url="https://github.com/owner/repo/actions/runs/123")

    def test_same_repository_uses_current_token(self):
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": CONTROLLER,
               "GITHUB_REF": "refs/heads/dev", "CURRENT_REPOSITORY_TOKEN": "current"}
        self.assertEqual(control.publication_env(self.source(), env)["GH_TOKEN"], "current")

    def test_cross_repository_publication_rejected(self):
        source = self.source()
        source["publish_repository"] = "upstream/components"
        env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": CONTROLLER,
               "GITHUB_REF": "refs/heads/dev", "CURRENT_REPOSITORY_TOKEN": "current"}
        with self.assertRaisesRegex(ValueError, "workflow repository"):
            control.publication_env(source, env)

    def test_other_workflow_branch_rejected(self):
        with self.assertRaisesRegex(ValueError, "dev workflow"):
            control.publication_env(self.source(), {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_REF": "refs/heads/other"})

    def test_other_controller_commit_rejected(self):
        with self.assertRaisesRegex(ValueError, "commit differs"):
            control.publication_env(self.source(), {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_REF": "refs/heads/dev", "GITHUB_SHA": "c" * 40})

    def test_unsafe_or_symlink_asset_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "target").write_text("data")
            (Path(temp) / "link").symlink_to("target")
            for name in ("../outside", "--option", "link"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    control.regular_asset(temp, name)

    def test_forged_provenance_rejected_before_source_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source()
            record = {"component": "dxvk", "artifact": {"name": "dxvk.zip"}, "provenance": {}}
            (root / "dxvk.zip").write_bytes(b"archive")
            control.write_json(root / "dxvk-component.json", record)
            module = Mock()
            with patch.object(control, "record_module", return_value=module):
                with self.assertRaisesRegex(ValueError, "provenance differs"):
                    control.load_records(root, source, root / "clean-source")
                module.verify_record.assert_not_called()
                record["provenance"] = {key: source[key] for key in (
                    "requested_repository", "requested_ref", "publish_repository", "release_tag",
                    "controller_repository", "controller_commit", "run_url")}
                control.write_json(root / "dxvk-component.json", record)
                control.load_records(root, source, root / "clean-source")
                module.verify_source.assert_called_once_with(root, record, root / "clean-source")

    def test_existing_release_refuses_any_mutation(self):
        source = self.source()
        existing = {"data": {"repository": {"ref": {"name": "graphics-test"}, "release": None}}}
        with patch.object(control, "validate_bundle", return_value=(source, [])), \
                patch.object(control, "publication_env", return_value={}), \
                patch.object(control, "run", return_value=json.dumps(existing)) as command:
            with self.assertRaisesRegex(ValueError, "already exists"):
                control.publish(Path("bundle"))
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[:3], ("gh", "api", "graphql"))

    def test_draft_verification_uses_numeric_release_endpoint(self):
        lookup = {"apiUrl": "https://api.github.com/repos/owner/repo/releases/123", "tagName": "graphics-test"}
        release = {"tag_name": "graphics-test", "draft": True, "assets": []}
        with patch.object(control, "run", return_value=json.dumps(lookup)), \
                patch.object(control, "api", return_value=release) as request:
            self.assertEqual(control.verify_remote_assets("owner/repo", "graphics-test", [], {}), release)
            request.assert_called_once_with("repos/owner/repo/releases/123", {})

    def test_draft_lookup_cannot_redirect_authenticated_verification(self):
        for url in ("https://evil.example/repos/owner/repo/releases/123",
                    "https://api.github.com/repos/other/repo/releases/123",
                    "https://api.github.com/repos/owner/repo/releases/123?redirect=1"):
            lookup = {"apiUrl": url, "tagName": "graphics-test"}
            with patch.object(control, "run", return_value=json.dumps(lookup)), \
                    patch.object(control, "api") as request:
                with self.assertRaisesRegex(ValueError, "unexpected identity"):
                    control.verify_remote_assets("owner/repo", "graphics-test", [], {})
                request.assert_not_called()

    def test_bundle_tamper_and_extra_assets_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source()
            control.write_json(root / "source.json", source)
            for name in ("release-pin.json", "adoption-manifest.json", "provenance.json", "dxvk-component.json"):
                (root / name).write_text("{}\n")
            (root / "dxvk.zip").write_bytes(b"archive")
            record = {"component": "dxvk", "artifact": {"name": "dxvk.zip"}}
            (root / "SHA256SUMS").write_text("".join(f"{control.digest(path)}  {path.name}\n" for path in sorted(root.iterdir())))
            with patch.object(control, "load_records", return_value=[record]):
                self.assertEqual(control.validate_bundle(root)[0], source)
                (root / "extra").write_text("unexpected")
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    control.validate_bundle(root)
                (root / "extra").unlink()
                (root / "dxvk.zip").write_bytes(b"modified")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    control.validate_bundle(root)

    def test_aggregate_writes_urls_only_to_destination_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            incoming = root / "incoming"
            incoming.mkdir()
            source = self.source()
            control.write_json(root / "source.json", source)
            record = {"component": "dxvk", "artifact": {"name": "dxvk.zip", "sha256": "d" * 64, "size": 7},
                      "recipe_inputs": {}, "pin": {"id": "dxvk", "sha256": "d" * 64, "size": 7}}
            control.write_json(incoming / "dxvk-component.json", record)
            (incoming / "dxvk.zip").write_bytes(b"archive")
            with patch.object(control, "load_records", return_value=[record]):
                control.aggregate(root / "source.json", incoming, root / "bundle", root / "clean-source")
            adoption = (root / "bundle/adoption-manifest.json").read_text()
            self.assertNotIn("https://", adoption)
            self.assertNotIn("owner/repo", adoption)
            pin = json.loads((root / "bundle/release-pin.json").read_text())
            self.assertEqual(pin["powervr"]["archives"][0]["url"], "https://github.com/owner/repo/releases/download/graphics-test/dxvk.zip")


if __name__ == "__main__":
    unittest.main()

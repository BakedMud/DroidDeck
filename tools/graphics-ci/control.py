#!/usr/bin/env python3
"""Resolve source, assemble component pins, and publish immutable CI releases."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


COMPONENTS = ("hybris", "dxvk", "wsi", "dxvk-xclipse", "dxvk-mali", "gamescope")
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def run(*args, env=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, env=env).stdout


def api(endpoint, env=None):
    return json.loads(run("gh", "api", "--hostname", "github.com", endpoint, env=env))


def repository(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository must be a GitHub owner/name")
    if value.split("/")[1] in (".", ".."):
        raise ValueError("invalid repository name")
    return value


def tag_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", value):
        raise ValueError("release tag contains unsupported characters")
    run("git", "check-ref-format", "refs/tags/" + value)
    return value


def selected_components(value):
    values = list(COMPONENTS) if value == "all" else value.split(",")
    if not values or len(set(values)) != len(values) or any(v not in COMPONENTS for v in values):
        raise ValueError("unknown or duplicate component selection")
    return values


def resolve_source(repo, ref):
    repo = repository(repo)
    if not ref or len(ref) > 512 or any(ord(c) < 32 for c in ref):
        raise ValueError("invalid source ref")
    pr = re.fullmatch(r"(?:pr:|#)?([1-9][0-9]*)", ref)
    if ref.startswith("https://"):
        match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?", ref)
        if not match:
            raise ValueError("PR URL must identify a github.com pull request")
        repo, number = repository(match[1]), match[2]
        pr = True
    elif pr:
        number = pr[1]
    elif "://" in ref:
        raise ValueError("unsupported source URL")
    if pr:
        result = api(f"repos/{repo}/pulls/{number}")
        head = result["head"]
        if not head.get("repo"):
            raise ValueError("PR head repository no longer exists")
        resolved_repo, commit = repository(head["repo"]["full_name"]), head["sha"]
    else:
        result = api(f"repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
        resolved_repo, commit = repo, result["sha"]
    if not SHA.fullmatch(commit):
        raise ValueError("GitHub did not return a full commit SHA")
    return resolved_repo, commit


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def resolve(output, env=os.environ):
    controller = repository(env["GITHUB_REPOSITORY"])
    requested_repo = env.get("SOURCE_REPOSITORY") or controller
    requested_ref = env.get("SOURCE_REF") or "dev"
    repo, commit = resolve_source(requested_repo, requested_ref)
    components = selected_components(env.get("COMPONENTS") or "all")
    release_tag = tag_name(env.get("RELEASE_TAG") or f"graphics-{commit[:12]}-{env['GITHUB_RUN_ID']}")
    destination = controller
    source = dict(format=1, repository=repo, commit=commit, components=components,
                  requested_repository=requested_repo, requested_ref=requested_ref,
                  publish_repository=destination, release_tag=release_tag,
                  controller_repository=controller, controller_commit=env["GITHUB_SHA"],
                  run_url=f"https://github.com/{controller}/actions/runs/{env['GITHUB_RUN_ID']}")
    validate_source(source)
    write_json(output, source)
    matrix = {"include": [{"component": component, "runner": "ubuntu-24.04-arm" if component in ("wsi", "gamescope") else "ubuntu-24.04"} for component in components]}
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a") as stream:
            for key, value in dict(repository=repo, commit=commit, release_tag=release_tag,
                                   publish_repository=destination, matrix=json.dumps(matrix)).items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(source, indent=2))


def validate_source(source):
    for key in ("repository", "publish_repository", "controller_repository"):
        repository(source[key])
    for key in ("commit", "controller_commit"):
        if not SHA.fullmatch(source[key]):
            raise ValueError(f"invalid {key}")
    if not source["components"] or selected_components(",".join(source["components"])) != source["components"]:
        raise ValueError("invalid component list")
    tag_name(source["release_tag"])


def record_module():
    spec = importlib.util.spec_from_file_location("graphics_component_record", Path(__file__).with_name("record.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def regular_asset(directory, name):
    if not ASSET.fullmatch(name):
        raise ValueError(f"invalid asset name: {name!r}")
    path = Path(directory) / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"asset is not a regular file: {name}")
    return path


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_records(directory, source, source_checkout=None):
    module = record_module()
    expected = {f"{component}-component.json" for component in source["components"]}
    actual = {p.name for p in Path(directory).glob("*-component.json")}
    if actual != expected:
        raise ValueError(f"component set mismatch: expected {sorted(expected)}, got {sorted(actual)}")
    records = []
    for component in source["components"]:
        record = json.loads(regular_asset(directory, component + "-component.json").read_text())
        if record["component"] != component:
            raise ValueError("component record filename mismatch")
        regular_asset(directory, record["artifact"]["name"])
        expected_provenance = {key: source[key] for key in (
            "requested_repository", "requested_ref", "publish_repository", "release_tag",
            "controller_repository", "controller_commit", "run_url")}
        if record["provenance"] != expected_provenance:
            raise ValueError("component provenance differs from the resolved source")
        module.verify_record(Path(directory), record, expected_source={"repository": source["repository"], "commit": source["commit"]})
        if source_checkout is not None:
            module.verify_source(Path(directory), record, Path(source_checkout))
        records.append(record)
    return records


def aggregate(source_record, input_dir, output, source_checkout):
    source = json.loads(Path(source_record).read_text())
    validate_source(source)
    records = load_records(input_dir, source, source_checkout)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("bundle output directory must be empty")
    pin = {"format": 1, "powervr": {"format": 1, "archives": []}}
    adoption = {"format": 1, "source_commit": source["commit"], "components": []}
    base_url = f"https://github.com/{source['publish_repository']}/releases/download/{urllib.parse.quote(source['release_tag'], safe='')}"
    for record in records:
        component = record["component"]
        for name in (record["artifact"]["name"], component + "-component.json"):
            if (out / name).exists():
                raise ValueError("duplicate output asset")
            shutil.copyfile(regular_asset(input_dir, name), out / name)
        component_pin = copy.deepcopy(record["pin"])
        adoption["components"].append({"component": component, "artifact": record["artifact"],
                                        "recipe_inputs": record["recipe_inputs"], "pin": copy.deepcopy(component_pin)})
        component_pin["url"] = base_url + "/" + record["artifact"]["name"]
        component_pin["provenance"] = {"repository_commit": source["commit"], "ci_run": source["run_url"]}
        if component == "gamescope":
            component_pin["repository"] = source["publish_repository"]
            component_pin["tag"] = source["release_tag"]
            pin["gamescope"] = component_pin
        elif component == "hybris":
            pin["powervr"]["hybris"] = component_pin
        else:
            pin["powervr"]["archives"].append(component_pin)
    # Adoption data deliberately contains no hosting repository or build-run URLs.
    if "https://" in json.dumps(adoption) or "http://" in json.dumps(adoption):
        raise ValueError("adoption manifest unexpectedly contains a URL")
    write_json(out / "source.json", source)
    write_json(out / "release-pin.json", pin)
    write_json(out / "adoption-manifest.json", adoption)
    write_json(out / "provenance.json", {"format": 1, "source": source, "components": records})
    (out / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(out.iterdir())))
    print(f"Validated {len(records)} components; bundle: {out}")


def validate_bundle(bundle):
    directory = Path(bundle)
    source = json.loads(regular_asset(directory, "source.json").read_text())
    validate_source(source)
    checksums = regular_asset(directory, "SHA256SUMS").read_text().splitlines()
    seen = set()
    for line in checksums:
        sha, name = line.split("  ", 1)
        if not SHA256.fullmatch(sha) or name in seen or name == "SHA256SUMS":
            raise ValueError("invalid checksum manifest")
        if digest(regular_asset(directory, name)) != sha:
            raise ValueError(f"checksum mismatch: {name}")
        seen.add(name)
    expected = {"source.json", "release-pin.json", "adoption-manifest.json", "provenance.json"}
    records = load_records(directory, source)
    for record in records:
        expected.update((record["component"] + "-component.json", record["artifact"]["name"]))
    if seen != expected or {p.name for p in directory.iterdir()} != expected | {"SHA256SUMS"}:
        raise ValueError("bundle has missing or unexpected files")
    return source, [regular_asset(directory, name) for name in sorted(expected | {"SHA256SUMS"})]


def publication_env(source, env=os.environ):
    current = repository(env["GITHUB_REPOSITORY"])
    if env.get("GITHUB_REF") != "refs/heads/dev":
        raise ValueError("component publication is restricted to the dev workflow")
    if source["controller_repository"].lower() != current.lower():
        raise ValueError("bundle was produced by another controller repository")
    if source["controller_commit"] != env["GITHUB_SHA"]:
        raise ValueError("bundle controller commit differs from this workflow")
    if source["publish_repository"].lower() != current.lower():
        raise ValueError("component publication is restricted to the workflow repository")
    key = "CURRENT_REPOSITORY_TOKEN"
    token = env.get(key)
    if not token:
        raise ValueError(f"{key} is required to publish to {source['publish_repository']}")
    result = dict(env)
    result["GH_TOKEN"] = token
    return result


def verify_remote_assets(repo, tag, assets, env):
    # REST get-by-tag exposes published releases only. gh can resolve a draft,
    # whose numeric release endpoint remains readable during verification.
    lookup = json.loads(run("gh", "release", "view", tag, "--repo", repo, "--json", "apiUrl,tagName", env=env))
    url = urllib.parse.urlsplit(lookup["apiUrl"])
    expected_path = re.escape(f"/repos/{repo}/releases/") + r"[1-9][0-9]*"
    if (lookup["tagName"] != tag or url.scheme != "https" or url.netloc != "api.github.com"
            or url.query or url.fragment or not re.fullmatch(expected_path, url.path, flags=re.IGNORECASE)):
        raise ValueError("release lookup returned an unexpected identity")
    release = api(url.path.lstrip("/"), env)
    if release["tag_name"] != tag:
        raise ValueError("release tag changed during verification")
    actual = {a["name"]: a for a in release["assets"]}
    if set(actual) != {p.name for p in assets}:
        raise ValueError("uploaded asset list differs from the validated bundle")
    with tempfile.TemporaryDirectory(prefix="graphics-release-") as temp:
        run("gh", "release", "download", tag, "--repo", repo, "--dir", temp, env=env)
        for path in assets:
            if actual[path.name]["size"] != path.stat().st_size or digest(Path(temp) / path.name) != digest(path):
                raise ValueError(f"uploaded bytes differ: {path.name}")
    return release


def verify_public_assets(repo, tag, assets):
    # Verify the URLs written into release-pin.json. A draft's download URLs can
    # use an untagged name, and newly published assets may not be visible yet.
    base = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}"
    delays = (0, 2, 5, 10, 20, 30)
    for path in assets:
        url = base + "/" + urllib.parse.quote(path.name, safe="")
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            request = urllib.request.Request(url, headers={"User-Agent": "DroidDeck-graphics-ci"})
            try:
                with urllib.request.urlopen(request, timeout=120) as stream:
                    sha = hashlib.file_digest(stream, "sha256").hexdigest()
            except urllib.error.HTTPError as error:
                error.close()
                retryable = error.code in (404, 408, 429) or 500 <= error.code < 600
                if not retryable or attempt == len(delays) - 1:
                    raise
                continue
            except (urllib.error.URLError, TimeoutError):
                if attempt == len(delays) - 1:
                    raise
                continue
            if sha != digest(path):
                raise ValueError(f"public release bytes differ: {path.name}")
            break


def publish(bundle):
    source, assets = validate_bundle(bundle)
    env = publication_env(source)
    repo, tag = source["publish_repository"], source["release_tag"]
    # Fail closed on API errors; a tag is immutable even if its release was removed.
    query = "query($owner:String!,$name:String!,$ref:String!,$tag:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){name} release(tagName:$tag){id}}}"
    owner, name = repo.split("/")
    existing = json.loads(run("gh", "api", "graphql", "-f", "query=" + query,
                              "-f", "owner=" + owner, "-f", "name=" + name,
                              "-f", "ref=refs/tags/" + tag, "-f", "tag=" + tag, env=env))
    if existing.get("errors") or not existing.get("data", {}).get("repository"):
        raise ValueError("could not establish release destination state")
    state = existing["data"]["repository"]
    if state["ref"] or state["release"]:
        raise ValueError("release tag already exists; choose a new immutable tag")
    with tempfile.TemporaryDirectory(prefix="graphics-notes-") as temp:
        body = Path(temp) / "notes.md"
        body.write_text(f"Graphics components built from `{source['repository']}@{source['commit']}`.\n\n"
                        f"Build: {source['run_url']}\n\n"
                        "SHA256SUMS and provenance.json describe the verified assets. "
                        "release-pin.json targets this release; adoption-manifest.json has no hosting URLs. "
                        "No application pin is changed automatically.\n")
        run("gh", "release", "create", tag, "--repo", repo, "--draft", "--prerelease",
            "--latest=false", "--title", tag, "--notes-file", str(body), env=env)
    run("gh", "release", "upload", tag, "--repo", repo, *map(str, assets), env=env)
    verify_remote_assets(repo, tag, assets, env)
    run("gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest=false", env=env)
    # Public URLs must work without an authentication token before reporting success.
    verify_public_assets(repo, tag, assets)
    print(f"Published and verified https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--output", required=True, type=Path)
    assembler = sub.add_parser("aggregate")
    assembler.add_argument("--source-record", required=True, type=Path)
    assembler.add_argument("--source", required=True, type=Path)
    assembler.add_argument("--input", required=True, type=Path)
    assembler.add_argument("--output", required=True, type=Path)
    publisher = sub.add_parser("publish")
    publisher.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "resolve":
        resolve(args.output)
    elif args.command == "aggregate":
        aggregate(args.source_record, args.input, args.output, args.source)
    else:
        publish(args.bundle)


if __name__ == "__main__":
    main()

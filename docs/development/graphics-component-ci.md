# Graphics component dispatch builds

The **Build game graphics components** workflow on `dev` can build recipes from a branch,
tag, commit, or pull request without adding temporary build workflows to that
source branch. The controller comes from the workflow commit; each source ref is
resolved once to its actual repository and full commit SHA.

```sh
gh workflow run build-game-graphics.yml --ref dev \
  -f source_ref=https://github.com/Droid-Deck/DroidDeck/pull/570 \
  -f components=all \
  -f publish=true
```

For a branch or commit in another repository, supply `source_repository=owner/repo`.
For a PR in the selected repository, use `source_ref=pr:570`. A full PR URL selects
its own repository, including the head repository when the PR comes from a fork.
The default source is `dev` in the workflow repository. That branch must contain
the recipes for the selected components; requesting absent recipes fails.

| Input | Meaning |
| --- | --- |
| `source_repository`, `source_ref` | Source repository and branch, tag, full SHA, `pr:NUMBER`, or PR URL |
| `components` | `all`, `hybris`, `dxvk`, `wsi`, `dxvk-xclipse`, `dxvk-mali`, or `gamescope` |
| `publish` | Default `false`: retain validated workflow artifacts only |
| `release_tag` | Default: `graphics-<source SHA prefix>-<run ID>`; existing tags are refused |

Dispatch and publication are restricted to `dev`, and releases always belong to
the workflow repository. The build jobs have read-only permissions and do not persist checkout credentials.
WSI and Gamescope build on ARM64; the other components build on x86-64. A separate
aggregation job verifies recipes against a fresh checkout without executing it.
The publisher executes controller code only, validates the component records again,
creates a draft prerelease, uploads all assets, compares downloaded bytes, and
publishes without changing the latest release. Public downloads are checked
without authentication. A failed upload or draft validation leaves the draft for
inspection; retry with a new tag rather than replacing published bytes.
If packaging fails after compilation, completed compressed archives are retained
for seven days in `graphics-raw-<component>` artifacts for diagnosis. Publication
still requires the normal component and clean-source validation steps.

```text
requested branch / commit / PR
              |
              v
     exact repository + SHA
              |
              v
   read-only component builds
              |
              v
  validated archives + records
              |
              v
 optional immutable prerelease
```

The `graphics-release-bundle` artifact and published release contain the archives,
individual component records, `SHA256SUMS`, `source.json`, `provenance.json`, and two
candidate pin documents:

* `release-pin.json` contains download URLs for this release. Its `powervr` object
  supplies component records for `tools/powervr/release.json`; `gamescope` supplies
  the Gamescope archive details. A partial build is a partial candidate, not a
  replacement for all existing pins.
* `adoption-manifest.json` contains archive hashes, file mappings, source commit,
  and recipe hashes without a hosting repository or build-run URL. Upstream can
  adopt the exact verified bytes into its own component repository, verify the
  public downloads, and only then add its own release URLs and provenance.

No application pin is changed automatically. Publishing on a fork does not make
its URLs suitable for an upstream application pin. Upstream publication requires
write access to the chosen upstream component repository. Rebuild the assembled
runtime after changing its inputs, and validate its manifest before updating an
application pin.

# Dependency and Toolchain Change

Theme id: `dependency-and-toolchain-change`. Lane for merges: B.

## Cues (for triage)

- Version numbers, git revisions, or checksums change in manifest or lockfile files (`package.json`, `pubspec.yaml`, `go.mod`, `pyproject.toml`, `Cargo.toml`).
- New third-party libraries, build plugins, linters, compilers, or SDK constraints are introduced.
- Build scripts, CI images, or bundler/transpiler flags change.

## Failure modes

1. The bumped dependency version introduces a breaking change in behavior, signature, or default options used by this repository.
   Look at: every call site of the bumped package in the tree and its installed declarations on disk. Settles it: `rg` imports and uses of the package and read its declaration or changelog; `Read` or `Fetched`.
2. The manifest constraint and the lockfile (or secondary manifest) disagree, leaving builds non-reproducible or resolving an unintended version.
   Look at: version ranges in the manifest versus resolved versions in the lockfile across workspace packages. Settles it: compare the manifest specifiers and lockfile entries directly; `Read`.
3. A compiler, SDK, or runtime bump changes default flags, strictness, or target output, breaking callers or deployment environments.
   Look at: language version constraints, target runtime floors, and build configuration defaults. Settles it: read the project's build/config files and check target environment constraints; `Read`.
4. Removing or replacing a dependency leaves transitive imports, runtime reflection targets, or build scripts referencing the removed package.
   Look at: source files, test helpers, scripts, and configs that imported or shelled out to the old package. Settles it: `rg` the package name and binary entry points across `HEAD`; `Read`.
5. A newly added or upgraded package introduces install hooks, native build steps, or a license incompatible with the repository.
   Look at: lockfile metadata, build scripts, and license files of the added dependency. Settles it: inspect the installed package manifest on disk or fetch its registry metadata; `Read` or `Fetched`.

## Hypothesis templates

- "Upgrading `<package>` from `<old_ver>` to `<new_ver>` changes `<symbol_or_option>` used at `<file:line>`."
- "Manifest `<manifest_file>` specifies `<range>` for `<package>`, but `<lockfile>` is not updated to match."
- "Removing `<package>` breaks `<caller_or_script>` at `<file:line>`, which still imports or invokes it directly."
- "Bumping the `<sdk_or_compiler>` minimum version in `<manifest>` exceeds the version pinned in `<ci_or_deploy_config>`."

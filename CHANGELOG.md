# Changelog

## 1.1.0

### dSYM upload now uses bugsee-cli

Symbol upload shells out to the [bugsee-cli](https://github.com/bugsee/bugsee-cli)
Rust binary instead of constructing a multipart zip and uploading
with `curl`. The same binary powers the Android Gradle plugin's
mapping upload — one upload mechanism, one wire format across both
platforms.

The CLI is downloaded automatically on first use from
`https://download.bugsee.com/cli`, SHA-256 verified, and cached at
`~/.bugsee/cli/<version>/<triple>/`. No new dependencies for the
consumer; subsequent runs hit the cache.

Override the auto-download:

- `BUGSEE_CLI_PATH=/path/to/bugsee-cli` — use a local binary
  (developer override, e.g. for testing an in-development CLI).
- `BUGSEE_CLI_VERSION=0.1.2` — pin or test a specific CLI release;
  defaults to the version bundled with this plugin release.

Each `.dSYM` is now uploaded as its own request rather than one
bundled zip — the Bugsee dashboard surfaces per-framework symbol
records as a result.

### iOS dependency collection

When invoked from an Xcode build phase, BugseeAgent now extracts
the project's dependency graph from `Podfile.lock`,
`Package.resolved`, and `Cartfile.resolved` and registers it with
the build. The emitted blob is wire-compatible with the Android
Gradle plugin's `DependencyCollector` (`schema_version=1`), so the
Bugsee dashboard, worker, and viewer render iOS deps the same way
they render Android deps.

Sources scanned (in this precedence order on duplicates):

1. **Podfile.lock** — the only source carrying a real graph. Pod
   subspecs declared explicitly in `pod ... :subspecs => [...]`
   are marked direct; umbrella pods reached transitively are not,
   matching CocoaPods' own user-intent model.
2. **Package.resolved** — both Xcode-managed
   (`{"object": {"pins": ...}}`) and SPM CLI v2 (`{"pins": ...}`)
   formats. Tagged version wins over revision when both are present
   on a pin.
3. **Cartfile.resolved** — `github`, `git`, and `binary` lines.

The list caps at 5000 entries (cross-platform contract with the
Android plugin's `DependencyPayloadSerializer.MAX_ENTRIES`); when
exceeded, the truncated flag flows through to the worker's
diff-compatibility check.

### VCS + build-machine metadata on build registration

Ported from the SDK's `tools.bundle/BugseeAgent`. The build
registration POST now carries:

- `vcs` sub-object with `provider` / `commit_sha` / `branch` /
  `base_branch` / `pr_number` / `repo`, populated from GitHub
  Actions, GitLab CI, or Bitbucket Pipelines env vars (provider-
  precedence in that order), with a `git` shell-out fallback for
  local archives. Same field names the Android Gradle plugin
  emits, so the dashboard renders iOS and Android builds with
  identical VCS context.
- `build_metadata.machine.host` now carries a CI-provider-aware
  label (`github-actions:<runner-name>`,
  `gitlab-ci:<runner-description>`, `jenkins:<node>`, `circleci:0`,
  `bitrise:<app-slug>`, `teamcity:<agent>`, `xcode-cloud:<workflow>`,
  generic `ci:<hostname>`) instead of just `platform.node()`. The
  dashboard's build-runner clustering now groups iOS + Android
  builds from the same runner.
- `build_metadata.build_system.version` is the dotted Xcode
  version (`16.2.0`) instead of the raw `XCODE_VERSION_ACTUAL`
  numeric form (`1620`). Falls back to `xcodebuild -version` when
  the env var is absent (CLI invocations outside Xcode's build
  phase).

These fields are accepted verbatim by the appserver's
`sanitizeVcs` / `sanitizeBuildMetadata`. The wire shape is
finalised by the Android Gradle plugin; the iOS side simply joins
the contract.

### iOS build timings via xcactivitylog

Ports the SLF (Source Log Format) tokenizer, section extractor,
category classifier, and timing orchestrator from the iOS SDK's
`tools.bundle/BugseeAgent` into the fastlane plugin's `BugseeAgent`
(~800 LOC verbatim port — code is battle-tested in the SDK, so
re-implementing would only create divergence).

The existing dependencies pipeline now ALSO collects per-task
build durations from the latest `.xcactivitylog` Xcode wrote
under `$DERIVED_DATA/Logs/Build/` and threads them into the
same build-registration round-trip:

- **Inline summary** — `build_metadata.timings` carries
  `total_ms`, `top_tasks`, and per-category sums
  (`native_ms` / `resources_ms` / `packaging_ms` / `other_ms`).
  `managed_code_ms` is never emitted on iOS (reserved for JVM
  bytecode pipelines on Android); the wire shape is identical
  between platforms so the back-end and the dashboard render
  both from one schema.
- **Detail blob** — the full per-task Gantt-chart-grade
  timeline is gzipped and PUT to the new `timings_upload_endpoint`
  the appserver returns alongside `dependencies_upload_endpoint`.

The deps and timings PUTs are independent: a failed deps upload
does NOT skip the timings upload (and vice versa), and either
PUT failing does NOT block the dSYM upload that runs afterward.

A pipeline run with NEITHER deps NOR timings produces no
network call (previously a missing lockfile alone would have
short-circuited even if timings were available).

Test coverage (+21 unittests, 161 total now):

  - `_classify_section_title` — 11 cases pinning the
    native / resources / packaging / managed_code mapping
    including the critical precedence carve-outs
    (`Compiling Clang module` → native, `LinkStoryboards` →
    resources NOT packaging, `Copy Swift standard libraries`
    → packaging NOT native).
  - `resolve_build_timings` soft-fail — empty env, missing
    `$OBJROOT`, no `Logs/Build/` dir all degrade to
    `(None, None)` rather than raising. A timings failure MUST
    NOT block the parent pipeline.
  - `_find_derived_data_root` — empty arg returns None, walk-up
    finds the root from a deep child, 10-step cap prevents
    walking to filesystem root on a malformed env.
  - `_find_latest_xcactivitylog` — no logs returns None,
    newest-mtime wins, descending-filename tie-break on equal
    mtimes (HFS+ second-granularity / rsync-preserving CI
    caches).

The full SLF binary tokenizer + section extractor are not
unit-tested here — they're best validated against real
xcactivitylog files (deferred to integration testing on the
SDK side, which has been parsing them in production for many
releases). The pieces tested above are the iOS-platform-specific
bits that don't depend on the binary format.

### Fixed

- `upload_symbols_to_bugsee`'s `symbol_maps:` parameter is now
  forwarded to BugseeAgent's `-m` flag. Previously the action
  referenced an undefined local `maps` variable and the parameter
  was silently dropped before reaching the agent.

### Test infrastructure

- 61-test Python `unittest` suite for BugseeAgent covering host
  triple detection, CLI download + SHA verification, dSYM upload
  argv shape, all three dependency-lockfile parsers, the merger
  + truncation, the payload builder, and gzip wire serialisation.
- 29-example RSpec suite for `upload_symbols_to_bugsee` covering
  the action's ConfigItem surface, shell-command marshaling,
  error handling, and platform support.
- GitHub Actions workflow runs both suites + RuboCop on every push
  and PR.

### Removed

- App icon attachment to dSYM uploads. The legacy curl pipeline
  shipped the Xcode-uncrushed launcher PNG inside the dSYM zip,
  which the worker extracted as `icon.source='build'`. The new
  per-dSYM `bugsee-cli` upload model has no slot for it
  (`--icon` is rejected for `--type dsym`), and the two helpers
  that produced the PNG (`getIcon` / `uncrushIcon`) have been
  removed from `BugseeAgent`.

  For apps published on the App Store / Google Play, the worker
  auto-fetches the icon (`icon.source='appstore'`) so the
  dashboard still renders an icon next to the app. For
  enterprise / TestFlight / internal builds that aren't on a
  public store, the dashboard will now render without an icon.
  Re-wiring icon attachment onto the build-registration body
  (`POST /v2/apps/<token>/builds`) is the planned path back —
  tracked separately from this release.

### Android mapping upload — `upload_mapping_to_bugsee`

New action covering two specific scenarios where the Bugsee Android
Gradle plugin alone isn't sufficient:

1. **Split build / publish CI.** The build stage runs gradle on
   machine A without the Bugsee app token; the publish stage runs
   fastlane on machine B with the production token and uploads the
   mapping. The action reads the Gradle plugin's
   `build-uuid.txt` (auto-globbed under
   `**/build/intermediates/bugsee/*/build-uuid.txt`) so the
   uploaded mapping is keyed by the same UUID the SDK already has
   baked into the APK.
2. **No Gradle plugin in the loop**, paired with Bugsee Android
   SDK 7.0.0-beta13+. The action synthesises a UUID Ruby-side via
   `UUID.nameUUIDFromBytes(app_token + 0x1F + version + 0x1F + build)`;
   the SDK reproduces the same UUID at runtime via its third
   BUILD_UUID fallback (Channel 3, added in 7.0.0-beta13).

The canonical case (Gradle plugin already in the loop, doing the
upload) remains untouched — this action complements rather than
replaces it.

UUID resolution chain: explicit `:uuid` > `:build_uuid_path` (if
the file exists) > globbed Gradle plugin `build-uuid.txt` > Ruby-side
synthesis matching the SDK Channel 3 formula. All four branches
produce a UUID the SDK can reproduce at runtime.

Wire-format implementation: the Ruby action shells to
`BugseeAgent --upload-mapping` (Python), which shells to
`bugsee-cli debug-files upload --type proguard --uuid <resolved>`.
The mapping upload reuses the same `bugsee-cli` resolver, cache
layout (`~/.bugsee/cli/<version>/<triple>/`), and SHA-256 verified
auto-download that powers the existing iOS dSYM path. No new CLI
dependency.

The Ruby-side UUID synthesis lives in
`Fastlane::Bugsee::Uuid.name_uuid_from_bytes` — a standalone helper
that mirrors Java's `UUID.nameUUIDFromBytes(bytes)` byte-for-byte
(MD5, version-3 + RFC-4122 variant bit twiddles). Cross-language
parity is locked in via a pinned reference vector in
`spec/bugsee_uuid_spec.rb` (Ruby), the Android SDK's
`BugseeEnvironmentBuildIdReaderTest.channel3_synthesisFormula_pinnedReferenceVector`
(Java), and the existing bugsee-cli `nameUUIDFromBytes` parity test
(Rust) — same input bytes, same expected UUID across all three.

`is_supported?(:android)` only; the existing
`upload_symbols_to_bugsee` remains `is_supported?(:ios)` only.

Test surface: +8 Python unittests in `TestUploadMappingViaCli`,
+47 RSpec examples across `spec/bugsee_uuid_spec.rb` and
`spec/upload_mapping_to_bugsee_spec.rb`. Combined suite: 140
Python + 79 RSpec = 219 tests, sub-200ms wall.

## 1.0.4

See git history for details on earlier releases.

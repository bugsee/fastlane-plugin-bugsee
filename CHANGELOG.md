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

### Scope clarification: Android mapping upload

The plugin remains iOS-only by design. Android mapping (ProGuard /
R8) upload stays with the
[Bugsee Android Gradle plugin](https://github.com/bugsee/bugsee-android-gradle-plugin),
not a parallel fastlane action — because the Bugsee Android SDK
learns the build UUID from channels only the Gradle plugin can
populate (post-R8 asset file + manifest meta-data fallback). A
fastlane-only mapping upload would land on the server but the SDK's
crash reports would carry no matching UUID, so symbolication would
never resolve.

A README section now points searchers in that direction so they
don't bounce off the plugin assuming Android isn't supported by
Bugsee at all.

## 1.0.4

See git history for details on earlier releases.

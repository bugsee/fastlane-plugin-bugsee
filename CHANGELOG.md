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

- App icon attachment to dSYM uploads — the legacy curl pipeline
  posted the app icon alongside the dSYM zip; `bugsee-cli` does
  not yet accept `--icon` for `--type dsym`. Planned for a later
  CLI release.

## 1.0.4

See git history for details on earlier releases.

# Bugsee fastlane plugin

[![fastlane Plugin Badge](https://rawcdn.githack.com/fastlane/fastlane/master/fastlane/assets/plugin-badge.svg)](https://rubygems.org/gems/fastlane-plugin-bugsee)

## Getting Started

This project is a [fastlane](https://github.com/fastlane/fastlane) plugin. To get started with `fastlane-plugin-bugsee`, add it to your project by running:

```bash
fastlane add_plugin bugsee
```

## About bugsee

Bugsee is free crash and bug reporting with video, network and logs. Sign up for a service at [https://www.bugsee.com](https://www.bugsee.com). This plugin implements a fastlane action that uploads debug symbol (dSYM) files to Bugsee, and — when invoked from an Xcode build phase — also collects and registers the project's dependency graph.

## Usage

For uploading symbols during build(gym) (non-bitcode case):
```
lane :mybuildlane do
  gym(
        # your settings for the bild
  )
  upload_symbols_to_bugsee(
        app_token: "<your bugsee app token>",
  )
end
```

For refreshing dSYM files from iTunes connect (bit-code case):
```
lane :refresh_dsyms do
  download_dsyms(
        build_number: "1819" # optional, otherwise it will download dSYM for all builds
  ) # Download dSYM files from iTC
  upload_symbols_to_bugsee(
        app_token: "<your bugsee app token>",
  )
  clean_build_artifacts           # Delete the local dSYM files
end
```

## How symbol upload works

Starting with `1.1.0`, symbol upload shells out to the [bugsee-cli](https://github.com/bugsee/bugsee-cli) Rust binary — the same uploader the Bugsee Android Gradle plugin uses for ProGuard/R8 mappings. One mechanism, one wire format across both platforms.

On first use the CLI is downloaded from `https://download.bugsee.com/cli`, SHA-256 verified against the published sidecar, and cached at `~/.bugsee/cli/<version>/<host-triple>/`. Subsequent runs hit the cache — no per-build network round-trip past the dSYM upload itself.

Override the auto-download with environment variables:

| Variable | Purpose |
| --- | --- |
| `BUGSEE_CLI_PATH` | Path to a local `bugsee-cli` binary. Useful when developing the CLI itself or in air-gapped CI. |
| `BUGSEE_CLI_VERSION` | Pin or test a specific CLI release; defaults to the version bundled with this plugin release. |

Each `.dSYM` is uploaded as its own request, so the dashboard surfaces a per-framework symbol record. If a host architecture isn't supported by the published CLI (currently macOS / Linux / Windows on x86_64 + arm64, where each platform exists), the agent logs and skips — it does not fail the build.

## Dependency collection

When `BugseeAgent` runs from an Xcode build phase (where `SRCROOT` / `INFOPLIST_PATH` are set), it also scans the project for dependency lockfiles and registers the resolved graph with the build:

- **CocoaPods** — `Podfile.lock` (provides direct/transitive distinction and parent edges).
- **Swift Package Manager** — `Package.resolved` (both Xcode-managed and SPM CLI v2 formats).
- **Carthage** — `Cartfile.resolved`.

The emitted blob is wire-compatible with the Bugsee Android Gradle plugin's `DependencyCollector`, so iOS and Android deps render identically in the dashboard.

No configuration is required — if a lockfile exists, it's collected.

## Documentation

Further documentation about Bugsee crash symbolication is available at https://docs.bugsee.com

## Issues and Feedback

For any other issues and feedback about this plugin, contact Bugsee support at support@bugsee.net.

## Troubleshooting

If you have trouble using plugins, check out the [Plugins Troubleshooting](https://docs.fastlane.tools/plugins/plugins-troubleshooting/) guide.

## Using `fastlane` Plugins

For more information about how the `fastlane` plugin system works, check out the [Plugins documentation](https://docs.fastlane.tools/plugins/create-plugin/).



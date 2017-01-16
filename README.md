# bugsee plugin

[![fastlane Plugin Badge](https://rawcdn.githack.com/fastlane/fastlane/master/fastlane/assets/plugin-badge.svg)](https://rubygems.org/gems/fastlane-plugin-bugsee)

## Getting Started

This project is a [fastlane](https://github.com/fastlane/fastlane) plugin. To get started with `fastlane-plugin-bugsee`, add it to your project by running:

```bash
fastlane add_plugin bugsee
```

## About bugsee

Bugsee is free crash and bug reporting with video, network and logs. Sign up for a service at [https://www.bugsee.com](https://www.bugsee.com). This plugin implements fastlane action to upload debug
symbol files to Bugsee servers.

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
lane :refresh_dsyms do
  download_dsyms(
        build_number: "1819" # optional, otherwise it will download all
  ) # Download dSYM files from iTC
  upload_symbols_to_bugsee(
        app_token: "<your bugsee app token>",
  )
  clean_build_artifacts           # Delete the local dSYM files
end

## Documentation

Further documentation about Bugsee crash symbolication is available at https://docs.bugsee.com

## Issues and Feedback

For any other issues and feedback about this plugin, contact Bugsee support at support@finik.net.

## Troubleshooting

If you have trouble using plugins, check out the [Plugins Troubleshooting](https://docs.fastlane.tools/plugins/plugins-troubleshooting/) guide.

## Using `fastlane` Plugins

For more information about how the `fastlane` plugin system works, check out the [Plugins documentation](https://docs.fastlane.tools/plugins/create-plugin/).



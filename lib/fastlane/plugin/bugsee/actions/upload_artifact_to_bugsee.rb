require 'fastlane/plugin/bugsee/helper/bugsee_handshake'

module Fastlane
  module Actions
    # Upload an iOS build artefact (`.app`) to Bugsee for size analysis.
    #
    # When and why to use this:
    #
    #   The Bugsee iOS SDK's `tools.bundle/BugseeAgent` already runs
    #   size analysis as a post-action build phase when
    #   `BUGSEE_BUILD_INFO_ENABLED` is on (default). The most common
    #   scenario for THIS fastlane action is CI pipelines that don't
    #   integrate the SDK's build phase (binary-only CI, alternative
    #   build systems) or that explicitly split build (machine A) from
    #   publish (machine B, has the production token).
    #
    # What it does:
    #
    #   1. Locates the `.app` — explicit `:app_path`, OR resolved from
    #      `:xcarchive_path` via `Products/Applications/<App>.app`.
    #   2. Shells to `BugseeAgent --upload-artifact` which:
    #      - Packages the `.app` into a synthetic byte-deterministic
    #        `.ipa` (same posture as the SDK side and the Android
    #        Gradle plugin so the back-end's content-hash dedup works
    #        across producers).
    #      - POSTs build registration to /v2/apps/<token>/builds with
    #        `request_artifact_upload: true`, getting back a presigned
    #        S3 PUT URL.
    #      - PUTs the .ipa bytes to that URL.
    #
    # Cross-producer handshake: if the Bugsee iOS SDK's BugseeAgent
    # build phase already uploaded this build's artefact, skip — same
    # gating shape as upload_symbols_to_bugsee and
    # upload_mapping_to_bugsee.
    class UploadArtifactToBugseeAction < Action
      BUGSEE_AGENT_PATH = File.expand_path(
        File.join(File.dirname(__FILE__), '..', '..', '..', '..', '..', 'BugseeAgent'))

      def self.run(params)
        app_token  = params[:app_token]
        host       = params[:host] || "https://api.bugsee.com"
        version    = params[:version]
        build      = params[:build]
        agent_path = params[:agent_path] || BUGSEE_AGENT_PATH

        UI.user_error!("Please provide an app token via app_token:") unless app_token

        agent_path = File.expand_path(agent_path)
        UI.user_error!("BugseeAgent helper script is missing: #{agent_path}") unless File.exist?(agent_path)

        # ──────────────────────────────────────────────────────
        # Resolve the `.app` path. Priority:
        #   1. Explicit :app_path
        #   2. :xcarchive_path → Products/Applications/<single .app>
        # ──────────────────────────────────────────────────────
        app_path = resolve_app_path(params)
        UI.user_error!(
          "Please provide either :app_path or :xcarchive_path (with a single .app inside Products/Applications/)"
        ) unless app_path
        UI.user_error!("App path does not exist: #{app_path}") unless File.directory?(app_path)
        UI.user_error!(
          "App path must end in .app: #{app_path}"
        ) unless app_path.end_with?('.app')

        # ──────────────────────────────────────────────────────
        # Cross-producer handshake
        # ──────────────────────────────────────────────────────
        # `artifact_upload` is the manifest action name the SDK
        # BugseeAgent writes when it successfully shipped the IPA.
        unless params[:force]
          manifest = Fastlane::Bugsee::Handshake.find_manifest(
            search_root: Dir.pwd,
            version_name: version,
            version_code: build,
          )
          if Fastlane::Bugsee::Handshake.handled_by_other?(manifest, 'artifact_upload')
            UI.important(Fastlane::Bugsee::Handshake.skip_message(manifest, 'artifact_upload'))
            return
          end
        end

        UI.message("Bugsee: uploading artefact for #{File.basename(app_path)}")

        # Shell command shape:
        #   python3 BugseeAgent -x \
        #     -e <host> [-v <ver>] [-b <build>] \
        #     --upload-artifact \
        #     --app-path <path> \
        #     <app_token>
        cmd = []
        cmd << agent_path.shellescape
        cmd << "-x"   # not run from Xcode — synchronous, no daemonize
        cmd << "-e #{host.shellescape}"
        cmd << "-v #{version.to_s.shellescape}" if version && !version.to_s.empty?
        cmd << "-b #{build.to_s.shellescape}"   if build   && !build.to_s.empty?
        cmd << "--upload-artifact"
        cmd << "--app-path #{app_path.shellescape}"
        # When build_info_only is true, skip the .ipa bytes upload —
        # the registration POST still records artifact_size on the
        # server so the dashboard's size-trend chart works, but the
        # bytes never leave the build host. Useful for firewalled CI
        # and privacy-sensitive setups.
        cmd << "--build-info-only" if params[:build_info_only]
        cmd << app_token.shellescape

        begin
          Actions.sh(cmd.join(" "), log: false)
        rescue => e
          # Upload failure should NOT take the lane down — size
          # analysis is a release-supporting feature, same posture as
          # the other Bugsee fastlane actions.
          UI.error(e.to_s)
        end
      end

      # @api private
      # Resolves the `.app` path the user wants packaged. Public-ish
      # so RSpec can exercise each branch in isolation.
      def self.resolve_app_path(params)
        explicit = params[:app_path]
        return explicit if explicit && !explicit.to_s.empty?

        archive_path = params[:xcarchive_path]
        return nil unless archive_path && !archive_path.to_s.empty?

        # An .xcarchive bundles the built `.app` at
        # `<archive>/Products/Applications/<App>.app`. Usually exactly
        # one `.app` is present; if multiple are present (extensions
        # are bundled inside the main .app's Frameworks/ subtree, not
        # a sibling at Products/Applications), we pick the only entry
        # and error if ambiguous so the user can disambiguate via
        # `:app_path`.
        apps_dir = File.join(archive_path, 'Products', 'Applications')
        return nil unless File.directory?(apps_dir)

        matches = Dir.entries(apps_dir).select do |e|
          e.end_with?('.app') && File.directory?(File.join(apps_dir, e))
        end
        if matches.length == 1
          return File.join(apps_dir, matches.first)
        elsif matches.length > 1
          UI.user_error!(
            "Multiple .app bundles found under #{apps_dir}; pass :app_path to disambiguate. Found: #{matches.inspect}"
          )
        end
        nil
      end

      def self.description
        "Upload an iOS build artefact (.app → synthetic .ipa) to Bugsee for size analysis."
      end

      def self.details
        <<~DETAILS
          Packages a built `.app` into a byte-deterministic synthetic `.ipa`
          and uploads it to Bugsee for size analysis. The Bugsee iOS SDK's
          tools.bundle/BugseeAgent build phase already does this when
          BUGSEE_BUILD_INFO_ENABLED is on (default); this action is for CI
          pipelines that don't integrate that build phase, or for
          split-build/publish setups where build and upload run on
          different machines.

          App path resolution: explicit :app_path > :xcarchive_path with a
          single .app inside Products/Applications/.

          Cross-producer handshake: if the SDK's BugseeAgent build phase
          already uploaded this build's artefact (recorded in the
          build-actions.json manifest), this action skips by default. Pass
          force: true to override.
        DETAILS
      end

      def self.available_options
        [
          FastlaneCore::ConfigItem.new(key: :agent_path,
                                       env_name: "BUGSEE_AGENT_PATH",
                                       description: "The path to the BugseeAgent helper script",
                                       optional: true,
                                       verify_block: proc do |value|
                                         UI.user_error!("Couldn't find BugseeAgent at path '#{value}'") unless File.exist?(value)
                                       end),
          FastlaneCore::ConfigItem.new(key: :host,
                                       env_name: "BUGSEE_API_HOST",
                                       description: "The path to API endpoint",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :app_token,
                                       env_name: "BUGSEE_APP_TOKEN",
                                       description: "Bugsee iOS application token",
                                       optional: false),
          FastlaneCore::ConfigItem.new(key: :app_path,
                                       env_name: "BUGSEE_APP_PATH",
                                       description: "Path to the built `.app` directory. When unset, resolved from :xcarchive_path",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :xcarchive_path,
                                       env_name: "BUGSEE_XCARCHIVE_PATH",
                                       description: "Path to the .xcarchive — used to resolve a single .app under Products/Applications/. Ignored when :app_path is set",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :version,
                                       env_name: "BUGSEE_APP_VERSION",
                                       description: "CFBundleShortVersionString (e.g. \"1.2.3\"). Optional — read from Info.plist by BugseeAgent if absent",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build,
                                       env_name: "BUGSEE_APP_BUILD",
                                       description: "CFBundleVersion (e.g. \"42\"). Optional — read from Info.plist by BugseeAgent if absent",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build_info_only,
                                       env_name: "BUGSEE_BUILD_INFO_ONLY",
                                       description: "When true, register the build (records artifact_size for the size-trend chart) but skip shipping the .ipa bytes. Useful for firewalled CI and privacy-sensitive setups",
                                       is_string: false,
                                       default_value: false,
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :force,
                                       env_name: "BUGSEE_FORCE",
                                       description: "Skip the cross-producer handshake check and always upload, even if the Bugsee iOS SDK's BugseeAgent build phase already handled this build's artefact",
                                       is_string: false,
                                       default_value: false,
                                       optional: true)
        ]
      end

      def self.authors
        ["bugsee"]
      end

      def self.is_supported?(platform)
        platform == :ios
      end
    end
  end
end

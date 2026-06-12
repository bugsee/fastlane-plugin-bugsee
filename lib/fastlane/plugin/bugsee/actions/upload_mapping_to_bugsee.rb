require 'fastlane/plugin/bugsee/helper/bugsee_uuid'
require 'fastlane/plugin/bugsee/helper/bugsee_handshake'

module Fastlane
  module Actions
    # Upload an Android ProGuard / R8 mapping.txt to Bugsee.
    #
    # When and why to use this:
    #
    #   The Bugsee Android Gradle plugin already uploads mapping.txt
    #   as part of the standard gradle build. The most common
    #   scenario for this fastlane action is CI pipelines that split
    #   build (machine A, no token) from publish (machine B, has the
    #   production token) — gradle builds the APK on A; fastlane on
    #   B uploads the mapping with the production token. The action
    #   ALSO works for apps not instrumented by the Bugsee Gradle
    #   plugin, but only when paired with Bugsee Android SDK
    #   7.0.0-beta13+ (older SDKs cannot match crashes against a
    #   synthesized UUID).
    #
    # UUID resolution chain (from most authoritative to fallback):
    #
    #   1. The :uuid ConfigItem when passed explicitly. Escape hatch.
    #   2. The Bugsee Gradle plugin's BugseeBuildIdResolveTask
    #      output file (build/intermediates/bugsee/<variant>/
    #      build-uuid.txt). When this file is present, the Gradle
    #      plugin IS in the loop and the UUID it computed is the
    #      authoritative value that the SDK's asset / manifest
    #      channels carry at runtime.
    #   3. Ruby-side synthesis matching the SDK's Channel 3
    #      fallback formula:
    #        UUID.nameUUIDFromBytes(
    #          app_token + 0x1F + version + 0x1F + build
    #        )
    #      Only useful when the host app uses SDK 7.0.0-beta13+
    #      AND has no Gradle plugin in the loop.
    #
    # All three branches produce a UUID the SDK can independently
    # reproduce at runtime, so the server-side mapping lookup
    # resolves crashes correctly regardless of which branch fired.
    class UploadMappingToBugseeAction < Action
      BUGSEE_AGENT_PATH = File.expand_path(
        File.join(File.dirname(__FILE__), '..', '..', '..', '..', '..', 'BugseeAgent'))

      # Glob pattern for the Bugsee Gradle plugin's
      # BugseeBuildIdResolveTask output. Used by the UUID resolution
      # chain when :build_uuid_path isn't explicit. The leading
      # `**/` covers multi-module projects where the `app/` module
      # nests under a top-level checkout.
      BUILD_UUID_GLOB = "**/build/intermediates/bugsee/*/build-uuid.txt".freeze

      def self.run(params)
        app_token    = params[:app_token]
        mapping_path = params[:mapping_path]
        host         = params[:host] || "https://api.bugsee.com"
        version      = params[:version]
        build        = params[:build]
        icon_path    = params[:icon_path]
        agent_path   = params[:agent_path] || BUGSEE_AGENT_PATH

        UI.user_error!("Please provide an app token via app_token:") unless app_token
        UI.user_error!("Please provide a path to the Android mapping.txt via mapping_path:") unless mapping_path
        UI.user_error!("Mapping file does not exist: #{mapping_path}") unless File.exist?(mapping_path)
        UI.user_error!("Please provide an app version via version: (android:versionName)") if version.nil? || version.to_s.empty?
        UI.user_error!("Please provide a build number via build: (android:versionCode)") if build.nil? || build.to_s.empty?

        agent_path = File.expand_path(agent_path)
        UI.user_error!("BugseeAgent helper script is missing: #{agent_path}") unless File.exist?(agent_path)

        if icon_path && !File.exist?(icon_path)
          UI.important("Bugsee: icon_path does not exist (#{icon_path}); proceeding without icon")
          icon_path = nil
        end

        # Cross-producer handshake: if the Bugsee Android Gradle
        # plugin already uploaded this build's mapping, skip — the
        # server would just dedupe by hash but the lane log gets
        # noisy and CI pays for redundant bandwidth. The user can
        # override with `force: true` for re-uploads / debugging.
        unless params[:force]
          manifest = Fastlane::Bugsee::Handshake.find_manifest(
            search_root: Dir.pwd,
            version_name: version,
            version_code: build,
          )
          if Fastlane::Bugsee::Handshake.handled_by_other?(manifest, 'mapping_upload')
            UI.important(Fastlane::Bugsee::Handshake.skip_message(manifest, 'mapping_upload'))
            return
          end
        end

        uuid = resolve_uuid(params, app_token, version, build)
        UI.message("Bugsee: uploading mapping with UUID #{uuid}")

        # Shell command shape:
        #   python3 BugseeAgent -x \
        #     -e <host> -v <version> -b <build> \
        #     --upload-mapping \
        #     --mapping-path <mapping.txt> \
        #     --mapping-uuid <uuid> \
        #     [--icon <icon>] \
        #     [--cli-path <path> | --cli-version <ver>] \
        #     <app_token>
        cmd = []
        cmd << agent_path.shellescape
        cmd << "-x"   # not run from Xcode — synchronous, no daemonize
        cmd << "-e #{host.shellescape}"
        cmd << "-v #{version.to_s.shellescape}"
        cmd << "-b #{build.to_s.shellescape}"
        cmd << "--upload-mapping"
        cmd << "--mapping-path #{mapping_path.shellescape}"
        cmd << "--mapping-uuid #{uuid.shellescape}"
        cmd << "--icon #{icon_path.shellescape}" if icon_path
        cmd << "--cli-path #{params[:cli_path].shellescape}" if params[:cli_path]
        cmd << "--cli-version #{params[:cli_version].shellescape}" if params[:cli_version]
        cmd << app_token.shellescape

        begin
          Actions.sh(cmd.join(" "), log: false)
        rescue => e
          # Upload failure should NOT take the lane down — the
          # symbols upload is a release-supporting nicety, same
          # posture as upload_symbols_to_bugsee. The Ruby UI.error
          # surfaces the cause in the build log.
          UI.error(e.to_s)
        end
      end

      # @api private
      # Resolution chain implemented as documented in the class
      # docstring above. Public-ish so the RSpec tests can exercise
      # each branch in isolation.
      def self.resolve_uuid(params, app_token, version, build)
        explicit = params[:uuid]
        return explicit if explicit && !explicit.to_s.empty?

        # Branch 2: Gradle plugin's BugseeBuildIdResolveTask output.
        # When :build_uuid_path is explicit, trust it. Otherwise glob
        # under the current working directory (most fastlane lanes
        # cd to the project root before invoking actions).
        build_uuid_path = params[:build_uuid_path]
        if build_uuid_path
          if File.exist?(build_uuid_path)
            return File.read(build_uuid_path).strip
          else
            UI.important("Bugsee: build_uuid_path given but not found: #{build_uuid_path}")
          end
        else
          # Best-effort glob. Take the first match — multi-variant
          # projects may have several; if the lane wants a specific
          # variant, it should pass :build_uuid_path explicitly.
          match = Dir.glob(BUILD_UUID_GLOB).first
          return File.read(match).strip if match
        end

        # Branch 3: synthesize from (app_token, version, build).
        # Matches the SDK's Channel 3 fallback formula byte-for-byte
        # so a 7.0.0-beta13+ SDK at runtime computes the same UUID
        # and the upload matches.
        UI.message("Bugsee: no Gradle plugin build-uuid.txt found; " \
                   "synthesizing UUID from app_token+version+build. " \
                   "This requires Bugsee Android SDK >= 7.0.0-beta13 " \
                   "at runtime.")
        Fastlane::Bugsee::Uuid.synthesize_build_uuid(app_token, version, build)
      end

      def self.description
        "Upload an Android ProGuard / R8 mapping.txt to Bugsee."
      end

      def self.details
        <<~DETAILS
          Uploads an Android ProGuard / R8 mapping.txt to Bugsee for crash
          symbolication. The Bugsee Android Gradle plugin already does this
          as part of the standard gradle build; this action is for split
          build/publish CI pipelines and for apps not instrumented by the
          Gradle plugin (the latter requires Bugsee Android SDK 7.0.0-beta13+).

          UUID resolution: explicit :uuid > :build_uuid_path > the Gradle
          plugin's build-uuid.txt (auto-globbed) > synthesized from
          (app_token, version, build) matching the SDK's runtime fallback.
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
                                       description: "Bugsee Android application token",
                                       optional: false),
          FastlaneCore::ConfigItem.new(key: :mapping_path,
                                       env_name: "BUGSEE_MAPPING_PATH",
                                       description: "Path to the Android mapping.txt (R8 / ProGuard)",
                                       optional: false),
          # Fastlane's Android `gradle` action publishes no
          # SharedValues equivalent of iOS's VERSION_NUMBER /
          # BUILD_NUMBER — :version / :build remain explicit
          # ConfigItems with no auto-default. The action validates
          # both in .run and raises a UI.user_error if missing.
          # Consumers typically wire these from get_version_name /
          # get_version_code (community actions) or read them from
          # build.gradle themselves before calling this action.
          FastlaneCore::ConfigItem.new(key: :version,
                                       env_name: "BUGSEE_APP_VERSION",
                                       description: "android:versionName (e.g. \"1.2.3\")",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build,
                                       env_name: "BUGSEE_APP_BUILD",
                                       description: "android:versionCode (e.g. \"42\")",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :uuid,
                                       env_name: "BUGSEE_BUILD_UUID",
                                       description: "Override BUILD_UUID. When unset, the action reads the Bugsee Gradle plugin's build-uuid.txt OR synthesizes via MD5",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build_uuid_path,
                                       env_name: "BUGSEE_BUILD_UUID_PATH",
                                       description: "Explicit path to the Bugsee Gradle plugin's build-uuid.txt. When unset, the action globs **/build/intermediates/bugsee/*/build-uuid.txt",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :icon_path,
                                       env_name: "BUGSEE_ICON_PATH",
                                       description: "Optional launcher icon PNG to attach to the symbol record",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :cli_path,
                                       env_name: "BUGSEE_CLI_PATH",
                                       description: "Path to a local bugsee-cli binary (developer override)",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :cli_version,
                                       env_name: "BUGSEE_CLI_VERSION",
                                       description: "bugsee-cli version to auto-download",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :force,
                                       env_name: "BUGSEE_FORCE",
                                       description: "Skip the cross-producer handshake check and always upload, even if the Bugsee Android Gradle plugin already handled this build's mapping",
                                       is_string: false,
                                       default_value: false,
                                       optional: true)
        ]
      end

      def self.authors
        ["bugsee"]
      end

      def self.is_supported?(platform)
        platform == :android
      end
    end
  end
end

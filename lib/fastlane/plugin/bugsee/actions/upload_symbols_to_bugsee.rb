require 'fastlane/plugin/bugsee/helper/bugsee_handshake'

module Fastlane
  module Actions

    # Upload iOS dSYM(s) to Bugsee, optionally collecting the
    # project's CocoaPods/SPM/Carthage dependency graph and the
    # latest Xcode xcactivitylog-derived build timings in the
    # same lane action.
    #
    # ## Cross-producer handshake
    #
    # When the Bugsee iOS SDK's `tools.bundle/BugseeAgent` build
    # phase is wired into the Xcode project, it runs alongside
    # fastlane and may already have handled some of these
    # actions for this build. Before invoking BugseeAgent, this
    # action reads the handshake manifest (see
    # `Fastlane::Bugsee::Handshake`) and skips per-action work
    # that's already been done.
    #
    # The handshake is checked for THREE independent actions:
    #
    #   - `dsym_upload`     — the primary purpose of this action.
    #   - `deps_collection` — Podfile/Package.resolved/Cartfile
    #                         parsing + upload.
    #   - `timings`         — xcactivitylog parsing + upload.
    #
    # When ALL three are handled by another producer for this
    # build, the action skips invoking BugseeAgent entirely. When
    # SOME are handled, the relevant `--no-*` flags are passed to
    # BugseeAgent so only the missing work runs.
    #
    # The `:force` ConfigItem skips the handshake check entirely.
    # Useful for re-uploads / debugging.
    class UploadSymbolsToBugseeAction < Action

      BUGSEE_AGENT_PATH = File.expand_path(
        File.join(File.dirname(__FILE__), '..', '..', '..', '..', '..', 'BugseeAgent'))

      def self.run(params)
        app_token = params[:app_token]
        host = params[:host] || "https://api.bugsee.com"
        agent_path = params[:agent_path] || BUGSEE_AGENT_PATH
        build_dir = params[:build_dir] || "./"

        UI.user_error!("Please provide an app token using app_token:") unless app_token
        UI.user_error!("Please provide a path to BugseeAgent helper script:") unless agent_path

        agent_path = File.expand_path(agent_path)
        UI.user_error!("BugseeAgent helper script is missing:") unless File.exist?(agent_path)

        dsym_path = params[:dsym_path]
        dsym_paths = params[:dsym_paths] || []
        if dsym_path
          dsym_paths += [dsym_path]
        end

        version = params[:version]
        build = params[:build]
        symbol_maps = params[:symbol_maps]

        dsym_paths.each do |path|
          print(path)
          UI.user_error!("dSYM does not exist at path: #{path}") unless File.exist?(path)
        end

        # ──────────────────────────────────────────────────────
        # Cross-producer handshake
        # ──────────────────────────────────────────────────────
        skip_dsym    = false
        skip_deps    = false
        skip_timings = false
        unless params[:force]
          manifest = Fastlane::Bugsee::Handshake.find_manifest(
            search_root: Dir.pwd,
            version_name: version,
            version_code: build,
          )
          skip_dsym    = Fastlane::Bugsee::Handshake.handled_by_other?(manifest, 'dsym_upload')
          skip_deps    = Fastlane::Bugsee::Handshake.handled_by_other?(manifest, 'deps_collection')
          skip_timings = Fastlane::Bugsee::Handshake.handled_by_other?(manifest, 'timings')

          [['dsym_upload',     skip_dsym],
           ['deps_collection', skip_deps],
           ['timings',         skip_timings]].each do |action_name, handled|
            next unless handled
            UI.important(Fastlane::Bugsee::Handshake.skip_message(manifest, action_name))
          end
        end

        # Strip dSYM paths when the handshake says they're already
        # handled — sending them along would make BugseeAgent
        # re-upload them since it interprets positional args as
        # dSYMs to push.
        effective_dsyms = skip_dsym ? [] : dsym_paths

        # When there's nothing for BugseeAgent to do at all,
        # short-circuit before constructing the command.
        if effective_dsyms.empty? && skip_deps && skip_timings
          UI.message("Bugsee: nothing to do — every action this lane would have run is already handled by another Bugsee producer for this build")
          return
        end

        # If we have NO dSYMs (either none provided OR all
        # handled) AND deps/timings have work to do, the BugseeAgent
        # invocation has to be non-dSYM. The agent's main() runs
        # the deps + timings pipeline unconditionally when
        # collect_deps is enabled and is told it's not dealing
        # with dSYMs — passing zero positional args + the
        # "external" -x flag is the existing shape for that.
        command = []
        command << agent_path.shellescape
        command << "-e #{host}"
        command << "-v #{version}" if version
        command << "-b #{build}" if build
        command << "-d #{build_dir}"
        command << "-m #{symbol_maps}" if symbol_maps
        command << "--no-deps" if skip_deps
        command << "--no-timings" if skip_timings
        command << "-x -l #{app_token}"
        command += effective_dsyms

        begin
          Actions.sh(command.join(" "), log: false)
        rescue => ex
          UI.error ex.to_s # it fails, however we don't want to fail everything just for this
        end
      end



      #####################################################
      # @!group Documentation
      #####################################################

      def self.description
        "Upload iOS dSYM(s) to Bugsee for crash symbolication. Also collects dependencies + build timings when not already handled by the Bugsee iOS SDK's tools.bundle/BugseeAgent build phase."
      end

      def self.details

      end

      def self.available_options
        [
          FastlaneCore::ConfigItem.new(key: :agent_path,
                                       env_name: "BUGSEE_AGENT_PATH",
                                       description: "The path to the BugseeAgent helper script",
                                       optional: true,
                                       verify_block: proc do |value|
                                         UI.user_error!("Couldn't find file at path '#{value}'") unless File.exist?(value)
                                       end),
          FastlaneCore::ConfigItem.new(key: :host,
                                       env_name: "BUGSEE_API_HOST",
                                       description: "The path to API endpoint",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :app_token,
                                       env_name: "BUGSEE_APP_TOKEN",
                                       description: "Bugsee Application token",
                                       optional: false),
          FastlaneCore::ConfigItem.new(key: :dsym_paths,
                                       env_name: "BUGSEE_DSYM_PATHS",
                                       description: "Array of zipped symbols files *.dSYM.zip",
                                       default_value: Actions.lane_context[SharedValues::DSYM_PATHS],
                                       is_string: false,
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :dsym_path,
                                       env_name: "BUGSEE_DSYM_PATH",
                                       description: "Path to а symbol file",
                                       default_value: Actions.lane_context[SharedValues::DSYM_OUTPUT_PATH],
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :symbol_maps,
                                       env_name: "BUGSEE_MAPS_PATH",
                                       description: "Path to а folder containing BCSymbolMaps",
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :version,
                                       env_name: "BUGSEE_APP_VERSION",
                                       description: "Application version",
                                       default_value: Actions.lane_context[SharedValues::VERSION_NUMBER],
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build,
                                       env_name: "BUGSEE_APP_BUILD",
                                       description: "Application build number",
                                       default_value: Actions.lane_context[SharedValues::BUILD_NUMBER],
                                       optional: true),
          FastlaneCore::ConfigItem.new(key: :build_dir,
                                        env_name: "TARGET_BUILD_DIR",
                                        description: "Target build directory",
                                        optional: true),
          FastlaneCore::ConfigItem.new(key: :force,
                                       env_name: "BUGSEE_FORCE",
                                       description: "Skip the cross-producer handshake check and run every action this lane was configured for, even if the Bugsee iOS SDK BugseeAgent build phase already handled some of them",
                                       is_string: false,
                                       default_value: false,
                                       optional: true)
        ]
      end

      def self.output
      end

      def self.return_value
      end

      def self.authors
        ["finik"]
      end

      def self.is_supported?(platform)
        platform == :ios
      end
    end
  end
end

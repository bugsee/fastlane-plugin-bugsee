module Fastlane
  module Actions

    class UploadSymbolsToBugseeAction < Action
      def self.run(params)
        app_token = params[:app_token]
        host = params[:host] || "https://api.bugsee.com"
        agent_path = params[:agent_path] || Dir["./**/BugseeAgent"].first
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
          UI.user_error!("dSYM does not exist at path: #{path}") unless File.exists?(path)
        end

        if dsym_paths.length > 0
          # Got here from download dsyms
          command = []
          command << agent_path.shellescape
          command << "-e #{host}"
          command << "-v #{version}" if version
          command << "-b #{build}" if build
          command << "-d #{build_dir}"
          command << "-m #{maps}" if symbol_maps
          command << "-x -l #{app_token}"
          command += dsym_paths
          begin
            Actions.sh(command.join(" "), log: false)
          rescue => ex
            UI.error ex.to_s # it fails, however we don't want to fail everything just for this
          end
        end
      end



      #####################################################
      # @!group Documentation
      #####################################################

      def self.description
        
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

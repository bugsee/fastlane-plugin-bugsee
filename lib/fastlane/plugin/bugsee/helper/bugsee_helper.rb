module Fastlane
  module Helper
    class BugseeHelper
      # class methods that you define here become available in your action
      # as `Helper::BugseeHelper.your_method`
      #
      def self.show_message
        UI.message("Hello from the bugsee plugin helper!")
      end
    end
  end
end

$LOAD_PATH.unshift File.expand_path('../../lib', __FILE__)

# This module is only used to check the environment is currently a testing env
module SpecHelper
end

require 'fastlane' # to import the Action super class
require 'fastlane/plugin/bugsee' # import the actual plugin

Fastlane.load_actions # load other actions (in case your plugin calls other actions or shared values)

# Integration specs (tagged `:integration`) drive the real bugsee-cli
# binary against a real local socket server. They require a CLI binary
# on disk (via BUGSEE_CLI_PATH) and take longer, so the default
# `bundle exec rspec` run EXCLUDES them. Opt in with RUN_INTEGRATION=1
# (CI does this in the dedicated `integration` job).
unless ENV['RUN_INTEGRATION']
  RSpec.configure do |config|
    config.filter_run_excluding(:integration)
  end
end

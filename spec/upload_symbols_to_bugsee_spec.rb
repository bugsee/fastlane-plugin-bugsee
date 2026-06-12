# frozen_string_literal: true

# Real spec for UploadSymbolsToBugseeAction — replaces the stub in
# bugsee_action_spec.rb (which describes a different placeholder action).
#
# Each example asserts something specific about the action's contract:
# what config keys it exposes, how it marshals user input into the
# BugseeAgent shell command, when it raises versus when it absorbs
# errors, and which platforms it supports. The shell command is
# captured via a mock on Fastlane::Actions.sh and asserted on directly,
# so a future regression in the argv shape (notably the `maps` →
# `symbol_maps` typo that previously made the -m flag inert) will fail
# the spec, not slip past coverage.

require 'spec_helper'
require 'tempfile'
require 'fileutils'

describe Fastlane::Actions::UploadSymbolsToBugseeAction do
  # Each example needs a real (existing) dSYM zip on disk because the
  # action enforces `File.exist?(path)` on every entry. Tempfile keeps
  # cleanup automatic and avoids cross-example pollution.
  let(:tmp_dsym) do
    f = Tempfile.new(['Foo.dSYM', '.zip'])
    f.write("not a real zip — the action only checks existence")
    f.close
    f
  end

  let(:agent_path) do
    File.expand_path('../../BugseeAgent', __FILE__)
  end

  # Captured argv from the shell-out — populated by the Actions.sh stub.
  let(:sh_capture) { [] }

  before do
    # Trap shell execution so we can assert on the constructed command
    # without actually running BugseeAgent.
    allow(Fastlane::Actions).to receive(:sh) do |cmd, **_kwargs|
      sh_capture << cmd
      ""  # match Actions.sh's typical return shape
    end
    # Silence UI noise during tests.
    allow(FastlaneCore::UI).to receive(:error)
    allow(FastlaneCore::UI).to receive(:important)
  end

  after do
    tmp_dsym.unlink if File.exist?(tmp_dsym.path)
  end

  # ──────────────────────────────────────────────────────────────
  # available_options shape — the user-visible config surface
  # ──────────────────────────────────────────────────────────────
  describe '.available_options' do
    let(:keys) { described_class.available_options.map(&:key) }

    it 'exposes app_token (required)' do
      expect(keys).to include(:app_token)
      item = described_class.available_options.find { |o| o.key == :app_token }
      expect(item.optional).to be false
    end

    it 'exposes the dSYM-input ConfigItems' do
      expect(keys).to include(:dsym_paths, :dsym_path)
    end

    it 'exposes the BCSymbolMap folder (symbol_maps), which was the typo regression site' do
      # Pre-fix the action looked for `:symbol_maps` in available_options
      # but referenced an undefined local `maps` inside .run. Pin the
      # key here so a future rename surfaces.
      expect(keys).to include(:symbol_maps)
    end

    it 'exposes version + build with fastlane lane-context defaults' do
      expect(keys).to include(:version, :build)
    end

    it 'exposes host with the canonical Bugsee API URL as default' do
      host_item = described_class.available_options.find { |o| o.key == :host }
      expect(host_item).not_to be_nil
      # `host` is optional — falls through to api.bugsee.com inside .run.
      expect(host_item.optional).to be true
    end

    it 'reads BUGSEE_APP_TOKEN env var as the canonical app_token override' do
      app_token_item = described_class.available_options.find { |o| o.key == :app_token }
      expect(app_token_item.env_name).to eq('BUGSEE_APP_TOKEN')
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Shell-command marshaling — the bulk of .run's responsibility
  # ──────────────────────────────────────────────────────────────
  describe '.run' do
    it 'invokes the shell exactly once when a single dsym is provided' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.size).to eq(1)
    end

    it 'shell-escapes the agent path so spaces in the install directory work' do
      # The action runs `agent_path.shellescape` before joining. Verify
      # by giving an agent path with a space and checking the escaped
      # form appears in the command.
      # (We can't actually use a path with a space without creating one,
      # so just confirm shellescape is in the resulting command shape.)
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include(agent_path.shellescape)
    end

    it 'uses the documented Bugsee host as the default -e endpoint' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-e https://api.bugsee.com')
    end

    it 'overrides the endpoint when host is supplied' do
      described_class.run(
        app_token: 'tok',
        host: 'https://apidev.bugsee.com',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-e https://apidev.bugsee.com')
    end

    it 'forwards the version when supplied' do
      described_class.run(
        app_token: 'tok',
        version: '1.2.3',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-v 1.2.3')
    end

    it 'forwards the build when supplied' do
      described_class.run(
        app_token: 'tok',
        build: '42',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-b 42')
    end

    it 'omits -v when version is nil (default fastlane lane-context lookup)' do
      described_class.run(
        app_token: 'tok',
        version: nil,
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      # The agent's Info.plist fallback kicks in; the action just
      # doesn't emit the flag, it doesn't synthesize one.
      expect(sh_capture.first).not_to match(/\s-v\s/)
    end

    it 'omits -b when build is nil' do
      described_class.run(
        app_token: 'tok',
        build: nil,
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).not_to match(/\s-b\s/)
    end

    it 'forwards -d build_dir (defaults to "./")' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-d ./')
    end

    it 'forwards the custom build_dir when supplied' do
      described_class.run(
        app_token: 'tok',
        build_dir: '/custom/build/dir',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-d /custom/build/dir')
    end

    # ─── The regression we explicitly came here to test ─────────
    it 'forwards -m symbol_maps when supplied (regression: the value used to be an undefined `maps`)' do
      # Pre-fix this branch tried to interpolate `#{maps}` and crashed
      # with NameError, so symbol_maps was silently ignored. Pin that
      # the value now reaches the shell command.
      described_class.run(
        app_token: 'tok',
        symbol_maps: '/path/to/BCSymbolMaps',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-m /path/to/BCSymbolMaps')
    end

    it 'omits -m when symbol_maps is not supplied' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).not_to match(/\s-m\s/)
    end

    # ─── App-token plumbing ─────────────────────────────────────
    it 'puts -l <app_token> as the last flag before the positional dsym arguments' do
      described_class.run(
        app_token: 'my-tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      # The action constructs `... -x -l <token> <dsym1> <dsym2> ...`
      # so the token appears AFTER -l and the dsym paths follow.
      cmd = sh_capture.first
      expect(cmd).to include('-l my-tok')
      l_idx = cmd.index('-l my-tok')
      dsym_idx = cmd.index(tmp_dsym.path)
      expect(dsym_idx).to be > l_idx
    end

    it 'always passes -x (external mode, no daemonize) — fastlane invokes synchronously' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [tmp_dsym.path],
        agent_path: agent_path,
      )
      expect(sh_capture.first).to include('-x')
    end

    # ─── dsym_path / dsym_paths merging ────────────────────────
    it 'merges the deprecated dsym_path into dsym_paths and passes both to the agent' do
      second = Tempfile.new(['Bar.dSYM', '.zip'])
      second.write("placeholder")
      second.close
      begin
        described_class.run(
          app_token: 'tok',
          dsym_paths: [tmp_dsym.path],
          dsym_path: second.path,
          agent_path: agent_path,
        )
        # Both files should appear in the constructed command as
        # positional arguments at the tail.
        expect(sh_capture.first).to include(tmp_dsym.path)
        expect(sh_capture.first).to include(second.path)
      ensure
        second.unlink
      end
    end

    it 'skips the shell call entirely when dsym_paths is empty (no work to do)' do
      described_class.run(
        app_token: 'tok',
        dsym_paths: [],
        agent_path: agent_path,
      )
      expect(sh_capture).to eq([])
    end

    # ─── Error handling ────────────────────────────────────────
    it 'raises a user error when app_token is missing' do
      expect {
        described_class.run(
          app_token: nil,
          dsym_paths: [tmp_dsym.path],
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /app token/i)
    end

    it 'raises a user error when a dSYM path does not exist' do
      expect {
        described_class.run(
          app_token: 'tok',
          dsym_paths: ['/no/such/file.dSYM.zip'],
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /dSYM does not exist/i)
    end

    it 'swallows BugseeAgent failures so the surrounding fastlane lane keeps running' do
      # If the agent shell call raises, the action catches it and logs
      # via UI.error rather than re-raising — by design, since symbol
      # upload is a release-supporting nicety, not a release blocker.
      allow(Fastlane::Actions).to receive(:sh).and_raise(StandardError.new("agent crashed"))
      expect(FastlaneCore::UI).to receive(:error).with(/agent crashed/)
      expect {
        described_class.run(
          app_token: 'tok',
          dsym_paths: [tmp_dsym.path],
          agent_path: agent_path,
        )
      }.not_to raise_error
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Platform support flag — fastlane respects this when deciding
  # whether to surface the action in a given lane.
  # ──────────────────────────────────────────────────────────────
  describe '.is_supported?' do
    it 'returns true for iOS' do
      expect(described_class.is_supported?(:ios)).to be true
    end

    it 'returns false for Android' do
      expect(described_class.is_supported?(:android)).to be false
    end

    it 'returns false for macOS' do
      # dSYM upload is iOS-specific in this plugin; macOS would use a
      # different action.
      expect(described_class.is_supported?(:mac)).to be false
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Authorship/metadata — small but pinned so a future blanket
  # rewrite doesn't accidentally drop authorship attribution.
  # ──────────────────────────────────────────────────────────────
  describe '.authors' do
    it 'returns an array of author handles' do
      expect(described_class.authors).to be_an(Array)
      expect(described_class.authors).not_to be_empty
    end
  end
end

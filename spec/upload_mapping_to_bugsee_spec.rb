# frozen_string_literal: true

# Real spec for the upload_mapping_to_bugsee action.
#
# Coverage focuses on what's distinct from upload_symbols_to_bugsee:
#   - The UUID resolution chain (explicit > Gradle plugin file > synthesis)
#   - The Android mapping shell-command shape (subset of BugseeAgent flags)
#   - Platform support predicate
#   - Critical edge cases (missing file, missing token, etc.)

require 'spec_helper'
require 'tempfile'
require 'tmpdir'
require 'fileutils'

describe Fastlane::Actions::UploadMappingToBugseeAction do
  let(:tmp_mapping) do
    f = Tempfile.new(['mapping', '.txt'])
    # The CLI cares about file existence; the bytes are immaterial
    # for the action layer (the bytes get read by bugsee-cli inside
    # the spawn).
    f.write("com.example.Foo -> a.b.c:\n")
    f.close
    f
  end

  let(:agent_path) do
    File.expand_path('../../BugseeAgent', __FILE__)
  end

  let(:sh_capture) { [] }

  before do
    allow(Fastlane::Actions).to receive(:sh) do |cmd, **_kwargs|
      sh_capture << cmd
      ""
    end
    # Silence noisy UI output during tests.
    allow(FastlaneCore::UI).to receive(:error)
    allow(FastlaneCore::UI).to receive(:important)
    allow(FastlaneCore::UI).to receive(:message)
  end

  after do
    tmp_mapping.unlink if File.exist?(tmp_mapping.path)
  end

  # ──────────────────────────────────────────────────────────────
  # available_options shape
  # ──────────────────────────────────────────────────────────────
  describe '.available_options' do
    let(:keys) { described_class.available_options.map(&:key) }

    it 'exposes app_token (required)' do
      expect(keys).to include(:app_token)
      item = described_class.available_options.find { |o| o.key == :app_token }
      expect(item.optional).to be false
    end

    it 'exposes mapping_path (required)' do
      expect(keys).to include(:mapping_path)
      item = described_class.available_options.find { |o| o.key == :mapping_path }
      expect(item.optional).to be false
    end

    it 'exposes the UUID resolution knobs' do
      expect(keys).to include(:uuid, :build_uuid_path)
    end

    it 'exposes version / build with Android lane-context defaults' do
      expect(keys).to include(:version, :build)
    end

    it 'exposes CLI override knobs' do
      expect(keys).to include(:cli_path, :cli_version)
    end

    it 'exposes icon_path (optional)' do
      expect(keys).to include(:icon_path)
    end
  end

  # ──────────────────────────────────────────────────────────────
  # .is_supported?
  # ──────────────────────────────────────────────────────────────
  describe '.is_supported?' do
    it 'returns true for Android' do
      expect(described_class.is_supported?(:android)).to be true
    end

    it 'returns false for iOS' do
      # The matching iOS action is upload_symbols_to_bugsee.
      expect(described_class.is_supported?(:ios)).to be false
    end

    it 'returns false for macOS' do
      expect(described_class.is_supported?(:mac)).to be false
    end
  end

  # ──────────────────────────────────────────────────────────────
  # UUID resolution chain
  # ──────────────────────────────────────────────────────────────
  describe '.resolve_uuid' do
    it 'wins on explicit :uuid' do
      params = double_params(uuid: 'explicit-uuid-value')
      result = described_class.resolve_uuid(params, 'tok', '1.0', '42')
      expect(result).to eq('explicit-uuid-value')
    end

    it 'reads :build_uuid_path when explicit' do
      Dir.mktmpdir do |dir|
        path = File.join(dir, 'build-uuid.txt')
        File.write(path, "gradle-emitted-uuid\n")
        params = double_params(build_uuid_path: path)
        result = described_class.resolve_uuid(params, 'tok', '1.0', '42')
        expect(result).to eq('gradle-emitted-uuid')
      end
    end

    it 'globs **/build/intermediates/bugsee/*/build-uuid.txt when no explicit path' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          variant_dir = File.join('app', 'build', 'intermediates', 'bugsee', 'release')
          FileUtils.mkdir_p(variant_dir)
          File.write(File.join(variant_dir, 'build-uuid.txt'), "globbed-uuid\n")

          params = double_params
          result = described_class.resolve_uuid(params, 'tok', '1.0', '42')
          expect(result).to eq('globbed-uuid')
        end
      end
    end

    it 'falls back to Ruby-side synthesis when no Gradle plugin file exists' do
      # Run in an empty temp dir so the glob misses, forcing the
      # synthesis branch. The expected value comes from the
      # documented reference vector — same one the SDK test pins.
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          params = double_params
          result = described_class.resolve_uuid(params, 'tok', '1.2.3', 42)
          expect(result).to eq('b27501e9-5a6a-3227-9f7d-f4696b48739c')
        end
      end
    end

    it 'falls back to synthesis when :build_uuid_path is given but missing' do
      # User passed a stale or wrong path. The action emits a
      # warning (verified separately via UI mock) but proceeds to
      # the synthesis branch rather than failing the build.
      params = double_params(build_uuid_path: '/nonexistent/build-uuid.txt')
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          result = described_class.resolve_uuid(params, 'tok', '1.2.3', 42)
          expect(result).to eq('b27501e9-5a6a-3227-9f7d-f4696b48739c')
        end
      end
    end

    it 'strips whitespace from the build-uuid.txt content' do
      # Real build-uuid.txt files often end with a newline. Pin that
      # we strip — a trailing newline in the UUID would break the
      # CLI's UUID parse.
      Dir.mktmpdir do |dir|
        path = File.join(dir, 'build-uuid.txt')
        File.write(path, "  uuid-with-pad  \n\n")
        params = double_params(build_uuid_path: path)
        result = described_class.resolve_uuid(params, 'tok', '1.0', '42')
        expect(result).to eq('uuid-with-pad')
      end
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Shell command marshaling
  # ──────────────────────────────────────────────────────────────
  describe '.run command shape' do
    it 'invokes BugseeAgent exactly once' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.2.3',
            build: '42',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.size).to eq(1)
    end

    it 'passes --upload-mapping flag' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('--upload-mapping')
    end

    it 'passes -v <version> and -b <build>' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.2.3',
            build: '42',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('-v 1.2.3')
      expect(sh_capture.first).to include('-b 42')
    end

    it 'passes the mapping file path via --mapping-path' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include("--mapping-path #{tmp_mapping.path}")
    end

    it 'passes the resolved UUID via --mapping-uuid' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.2.3',
            build: '42',
            uuid: 'explicit-uuid',  # forces UUID resolution to the explicit branch
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('--mapping-uuid explicit-uuid')
    end

    it 'omits --icon when icon_path is not provided' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).not_to match(/\s--icon\s/)
    end

    it 'forwards --icon when icon_path exists' do
      icon = Tempfile.new(['icon', '.png'])
      icon.write("\x89PNG\r\n\x1a\n")
      icon.close
      begin
        Dir.mktmpdir do |dir|
          Dir.chdir(dir) do
            described_class.run(
              app_token: 'tok',
              mapping_path: tmp_mapping.path,
              version: '1.0',
              build: '1',
              icon_path: icon.path,
              agent_path: agent_path,
            )
          end
        end
        expect(sh_capture.first).to include("--icon #{icon.path}")
      ensure
        icon.unlink
      end
    end

    it 'forwards --cli-path when explicit' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            cli_path: '/dev/bin/bugsee-cli',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('--cli-path /dev/bin/bugsee-cli')
    end

    it 'forwards --cli-version when explicit' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            cli_version: '0.2.0',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('--cli-version 0.2.0')
    end

    it 'passes -x (synchronous, no daemonize)' do
      # Android fastlane lanes WANT to block on the upload — the
      # iOS-style double-fork daemonization is the wrong default.
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('-x')
    end

    it 'puts the app_token as the LAST positional argument' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'my-tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      # BugseeAgent's optparse takes the app_token as args[0] AFTER
      # all the flags. A regression that put the token before a
      # flag would either consume the token as a flag value or be
      # rejected at parse time.
      cmd = sh_capture.first
      expect(cmd).to end_with(' my-tok')
    end

    it 'uses default Bugsee host when :host not specified' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('-e https://api.bugsee.com')
    end

    it 'overrides host when explicit' do
      Dir.mktmpdir do |dir|
        Dir.chdir(dir) do
          described_class.run(
            app_token: 'tok',
            mapping_path: tmp_mapping.path,
            version: '1.0',
            build: '1',
            host: 'https://apidev.bugsee.com',
            agent_path: agent_path,
          )
        end
      end
      expect(sh_capture.first).to include('-e https://apidev.bugsee.com')
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Error handling
  # ──────────────────────────────────────────────────────────────
  describe '.run error handling' do
    it 'raises when app_token is missing' do
      expect {
        described_class.run(
          mapping_path: tmp_mapping.path,
          version: '1.0',
          build: '1',
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /app token/i)
    end

    it 'raises when mapping_path is missing' do
      expect {
        described_class.run(
          app_token: 'tok',
          version: '1.0',
          build: '1',
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /mapping/i)
    end

    it 'raises when mapping file does not exist' do
      expect {
        described_class.run(
          app_token: 'tok',
          mapping_path: '/no/such/mapping.txt',
          version: '1.0',
          build: '1',
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /does not exist/i)
    end

    it 'raises when version is missing' do
      expect {
        described_class.run(
          app_token: 'tok',
          mapping_path: tmp_mapping.path,
          build: '1',
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /version/i)
    end

    it 'raises when build is missing' do
      expect {
        described_class.run(
          app_token: 'tok',
          mapping_path: tmp_mapping.path,
          version: '1.0',
          agent_path: agent_path,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /build/i)
    end

    it 'swallows BugseeAgent shell failures so the lane keeps running' do
      # Symbol upload is release-supporting, not release-blocking —
      # same posture as upload_symbols_to_bugsee.
      allow(Fastlane::Actions).to receive(:sh).and_raise(StandardError.new("agent crashed"))
      expect(FastlaneCore::UI).to receive(:error).with(/agent crashed/)
      expect {
        Dir.mktmpdir do |dir|
          Dir.chdir(dir) do
            described_class.run(
              app_token: 'tok',
              mapping_path: tmp_mapping.path,
              version: '1.0',
              build: '1',
              agent_path: agent_path,
            )
          end
        end
      }.not_to raise_error
    end
  end

  # ──────────────────────────────────────────────────────────────
  # helpers
  # ──────────────────────────────────────────────────────────────

  # Build a params double that responds to [:key] like
  # FastlaneCore::Configuration. Keeps the tests focused on the
  # resolve_uuid logic rather than the ConfigItem wiring.
  def double_params(opts = {})
    h = { uuid: nil, build_uuid_path: nil }.merge(opts)
    obj = Object.new
    obj.define_singleton_method(:[]) { |k| h[k] }
    obj
  end
end

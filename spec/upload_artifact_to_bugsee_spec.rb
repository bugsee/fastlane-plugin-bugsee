# frozen_string_literal: true

# Real spec for the upload_artifact_to_bugsee action.
#
# Coverage focuses on what's distinct from upload_symbols_to_bugsee /
# upload_mapping_to_bugsee:
#   - App-path resolution chain (:app_path > :xcarchive_path → Products/Applications/<App>.app)
#   - Shell-command shape for the BugseeAgent --upload-artifact flow
#   - Platform support predicate (iOS only)
#   - Handshake skip on `artifact_upload`

require 'spec_helper'
require 'tempfile'
require 'tmpdir'
require 'fileutils'

describe Fastlane::Actions::UploadArtifactToBugseeAction do
  let(:agent_path) do
    File.expand_path('../../BugseeAgent', __FILE__)
  end

  let(:sh_capture) { [] }

  before do
    allow(Fastlane::Actions).to receive(:sh) do |cmd, **_kwargs|
      sh_capture << cmd
      ""
    end
    allow(FastlaneCore::UI).to receive(:error)
    allow(FastlaneCore::UI).to receive(:important)
    allow(FastlaneCore::UI).to receive(:message)
  end

  # Helper: stand up a temp `.app` directory the action can package.
  def make_app(parent)
    app = File.join(parent, 'Foo.app')
    FileUtils.mkdir_p(app)
    File.write(File.join(app, 'Info.plist'),
               '<plist><dict><key>X</key><string>Y</string></dict></plist>')
    File.write(File.join(app, 'Foo'), 'macho-stub')
    app
  end

  # Helper: stand up a temp .xcarchive that nests one .app under
  # Products/Applications/.
  def make_xcarchive(parent, app_name: 'Foo.app')
    archive = File.join(parent, 'My.xcarchive')
    apps_dir = File.join(archive, 'Products', 'Applications')
    FileUtils.mkdir_p(apps_dir)
    app = File.join(apps_dir, app_name)
    FileUtils.mkdir_p(app)
    File.write(File.join(app, 'Info.plist'), '<plist/>')
    [archive, app]
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

    it 'exposes app_path AND xcarchive_path (both optional — at least one resolved at runtime)' do
      expect(keys).to include(:app_path, :xcarchive_path)
      app_path_item       = described_class.available_options.find { |o| o.key == :app_path }
      xcarchive_path_item = described_class.available_options.find { |o| o.key == :xcarchive_path }
      expect(app_path_item.optional).to be true
      expect(xcarchive_path_item.optional).to be true
    end

    it 'exposes version / build (optional — read from Info.plist by agent if absent)' do
      expect(keys).to include(:version, :build)
    end

    it 'exposes force (default false, boolean)' do
      expect(keys).to include(:force)
      item = described_class.available_options.find { |o| o.key == :force }
      expect(item.default_value).to be false
    end

    it 'exposes build_info_only (default false, boolean)' do
      expect(keys).to include(:build_info_only)
      item = described_class.available_options.find { |o| o.key == :build_info_only }
      expect(item.default_value).to be false
    end
  end

  # ──────────────────────────────────────────────────────────────
  # .is_supported? — iOS only
  # ──────────────────────────────────────────────────────────────
  describe '.is_supported?' do
    it 'returns true for iOS' do
      expect(described_class.is_supported?(:ios)).to be true
    end

    it 'returns false for Android' do
      expect(described_class.is_supported?(:android)).to be false
    end

    it 'returns false for macOS' do
      expect(described_class.is_supported?(:mac)).to be false
    end
  end

  # ──────────────────────────────────────────────────────────────
  # .resolve_app_path — resolution chain pins
  # ──────────────────────────────────────────────────────────────
  describe '.resolve_app_path' do
    it 'prefers explicit :app_path over :xcarchive_path' do
      Dir.mktmpdir do |tmp|
        explicit_app = make_app(tmp)
        archive, _arch_app = make_xcarchive(tmp)
        result = described_class.resolve_app_path(
          app_path: explicit_app,
          xcarchive_path: archive,
        )
        expect(result).to eq(explicit_app)
      end
    end

    it 'falls back to :xcarchive_path when :app_path is empty' do
      Dir.mktmpdir do |tmp|
        archive, arch_app = make_xcarchive(tmp)
        result = described_class.resolve_app_path(
          app_path: '',
          xcarchive_path: archive,
        )
        expect(result).to eq(arch_app)
      end
    end

    it 'returns nil when neither :app_path nor :xcarchive_path resolves' do
      result = described_class.resolve_app_path(
        app_path: nil,
        xcarchive_path: nil,
      )
      expect(result).to be_nil
    end

    it 'returns nil when :xcarchive_path lacks Products/Applications/' do
      Dir.mktmpdir do |tmp|
        archive = File.join(tmp, 'Empty.xcarchive')
        FileUtils.mkdir_p(archive)
        result = described_class.resolve_app_path(
          xcarchive_path: archive,
        )
        expect(result).to be_nil
      end
    end

    it 'raises a user error when multiple .app bundles are present (ambiguous)' do
      Dir.mktmpdir do |tmp|
        archive, _ = make_xcarchive(tmp, app_name: 'Foo.app')
        # Drop a second .app sibling to create ambiguity.
        second = File.join(archive, 'Products', 'Applications', 'Bar.app')
        FileUtils.mkdir_p(second)
        expect {
          described_class.resolve_app_path(xcarchive_path: archive)
        }.to raise_error(FastlaneCore::Interface::FastlaneError, /Multiple .app/)
      end
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Shell command shape — pins what BugseeAgent is invoked with
  # ──────────────────────────────────────────────────────────────
  describe 'BugseeAgent shell invocation' do
    it 'invokes --upload-artifact with --app-path and the token' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'test-token',
          app_path: app,
          agent_path: agent_path,
          version: '1.2.3',
          build: '42',
          force: true,
        )
        expect(sh_capture.length).to eq(1)
        cmd = sh_capture.first
        expect(cmd).to include('--upload-artifact')
        expect(cmd).to include("--app-path")
        expect(cmd).to include(app)
        # Token is the trailing positional arg.
        expect(cmd).to end_with('test-token')
        # Synchronous flag — no daemonization.
        expect(cmd).to include('-x')
      end
    end

    it 'passes --version and --build through when provided' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          version: '2.5.0',
          build: '99',
          force: true,
        )
        cmd = sh_capture.first
        expect(cmd).to include('-v 2.5.0')
        expect(cmd).to include('-b 99')
      end
    end

    it 'omits -v / -b when not provided (agent reads them from Info.plist)' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          force: true,
        )
        cmd = sh_capture.first
        expect(cmd).not_to include(' -v ')
        expect(cmd).not_to include(' -b ')
      end
    end

    it 'passes --build-info-only when build_info_only: true' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          build_info_only: true,
          force: true,
        )
        expect(sh_capture.first).to include('--build-info-only')
      end
    end

    it 'omits --build-info-only when the flag is unset (default: ship bytes)' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          force: true,
        )
        expect(sh_capture.first).not_to include('--build-info-only')
      end
    end

    it 'uses the custom :host when provided' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          host: 'https://staging.bugsee.com',
          force: true,
        )
        cmd = sh_capture.first
        expect(cmd).to include('-e https://staging.bugsee.com')
      end
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Validation — fail fast before shelling to BugseeAgent
  # ──────────────────────────────────────────────────────────────
  describe 'input validation' do
    it 'raises when app_token is missing' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        expect {
          described_class.run(
            app_token: nil,
            app_path: app,
            agent_path: agent_path,
            force: true,
          )
        }.to raise_error(FastlaneCore::Interface::FastlaneError, /app token/i)
      end
    end

    it 'raises when neither :app_path nor :xcarchive_path resolves to a .app' do
      expect {
        described_class.run(
          app_token: 'tok',
          agent_path: agent_path,
          force: true,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /app_path.*xcarchive_path/)
    end

    it 'raises when the resolved app path does not exist' do
      expect {
        described_class.run(
          app_token: 'tok',
          app_path: '/no/such/path.app',
          agent_path: agent_path,
          force: true,
        )
      }.to raise_error(FastlaneCore::Interface::FastlaneError, /does not exist/)
    end

    it 'raises when the resolved app path does not end in .app' do
      Dir.mktmpdir do |tmp|
        wrong = File.join(tmp, 'NotAnApp.bundle')
        FileUtils.mkdir_p(wrong)
        expect {
          described_class.run(
            app_token: 'tok',
            app_path: wrong,
            agent_path: agent_path,
            force: true,
          )
        }.to raise_error(FastlaneCore::Interface::FastlaneError, /must end in .app/)
      end
    end
  end

  # ──────────────────────────────────────────────────────────────
  # Handshake — skip when SDK BugseeAgent already shipped the IPA
  # ──────────────────────────────────────────────────────────────
  describe 'cross-producer handshake' do
    it 'skips invocation when artifact_upload is handled by another producer' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        # Stub the handshake helper to claim the build is already done.
        fake_manifest = { 'foo' => 'bar' }
        allow(Fastlane::Bugsee::Handshake).to receive(:find_manifest).and_return(fake_manifest)
        allow(Fastlane::Bugsee::Handshake).to receive(:handled_by_other?)
          .with(fake_manifest, 'artifact_upload').and_return(true)
        allow(Fastlane::Bugsee::Handshake).to receive(:skip_message)
          .and_return('Bugsee: artifact_upload already handled by SDK')

        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
        )
        # No shell-out — the handshake short-circuited.
        expect(sh_capture).to be_empty
      end
    end

    it 'still shells out when force: true overrides the handshake' do
      Dir.mktmpdir do |tmp|
        app = make_app(tmp)
        allow(Fastlane::Bugsee::Handshake).to receive(:find_manifest).and_return({})
        allow(Fastlane::Bugsee::Handshake).to receive(:handled_by_other?).and_return(true)
        described_class.run(
          app_token: 'tok',
          app_path: app,
          agent_path: agent_path,
          force: true,
        )
        # force: true bypasses the handshake check entirely.
        expect(sh_capture.length).to eq(1)
      end
    end
  end
end

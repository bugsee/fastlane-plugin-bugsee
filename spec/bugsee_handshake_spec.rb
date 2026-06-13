# frozen_string_literal: true

# Tests for the cross-producer build-action handshake reader.
#
# The handshake is the load-bearing contract between the Bugsee
# Android Gradle plugin, the Bugsee iOS SDK's tools.bundle/BugseeAgent
# build phase, and this fastlane plugin. Drift in the schema or in
# the staleness/identity-match logic silently breaks per-action
# de-duplication, so every behaviour gets a pinning assertion here.

require 'fastlane/plugin/bugsee/helper/bugsee_handshake'
require 'tmpdir'
require 'fileutils'
require 'json'

describe Fastlane::Bugsee::Handshake do
  let(:now) { Time.at(1_700_000_000) }

  # Build a manifest with sensible defaults and let each test
  # override what it cares about. Always returns a fresh Hash so
  # tests can mutate without cross-contamination.
  def manifest(overrides = {})
    {
      'schema_version'   => 1,
      'producer'         => 'bugsee-android-gradle-plugin',
      'producer_version' => '1.2.3',
      'build_id'         => 'b27501e9-5a6a-3227-9f7d-f4696b48739c',
      'produced_at_ms'   => (now.to_f * 1000).to_i,
      'version_name'     => '1.2.3',
      'version_code'     => '42',
      'actions' => {
        'mapping_upload'  => true,
        'dsym_upload'     => false,
        'deps_collection' => true,
        'timings'         => true,
        'size_analysis'   => false,
      },
    }.merge(overrides)
  end

  def write_manifest(dir, content, glob_subdir = 'app/build/intermediates/bugsee/release')
    full_dir = File.join(dir, glob_subdir)
    FileUtils.mkdir_p(full_dir)
    path = File.join(full_dir, 'build-actions.json')
    File.write(path, content.is_a?(String) ? content : JSON.dump(content))
    path
  end

  # ──────────────────────────────────────────────────────────────
  # find_manifest — locating the right file
  # ──────────────────────────────────────────────────────────────
  describe '.find_manifest' do
    it 'returns nil when no manifest exists' do
      Dir.mktmpdir do |dir|
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result).to be_nil
      end
    end

    it 'reads an Android-style manifest from intermediates/bugsee/<variant>/' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest)
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result['producer']).to eq('bugsee-android-gradle-plugin')
      end
    end

    it 'reads an iOS-style manifest from build/bugsee/' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest('producer' => 'bugsee-ios-sdk-tools-bundle'),
                       'MyApp/build/bugsee')
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result['producer']).to eq('bugsee-ios-sdk-tools-bundle')
      end
    end

    it 'when multiple manifests exist, picks the newest by mtime' do
      Dir.mktmpdir do |dir|
        old_path = write_manifest(dir,
                                  manifest('producer' => 'old',
                                           # produced_at_ms inside window
                                           'produced_at_ms' => (now.to_f * 1000).to_i - 500),
                                  'app/build/intermediates/bugsee/debug')
        new_path = write_manifest(dir,
                                  manifest('producer' => 'new'),
                                  'app/build/intermediates/bugsee/release')
        # Force mtimes so the test isn't FS-timing dependent.
        FileUtils.touch(old_path, mtime: now - 60)
        FileUtils.touch(new_path, mtime: now)

        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result['producer']).to eq('new')
      end
    end

    it 'rejects a manifest older than the staleness window' do
      Dir.mktmpdir do |dir|
        # Manifest produced > 1h ago. Within file freshness, but
        # the staleness check uses produced_at_ms, not mtime, so
        # this MUST be rejected.
        write_manifest(dir, manifest(
          'produced_at_ms' => (now.to_f * 1000).to_i - (61 * 60 * 1000),
        ))
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result).to be_nil
      end
    end

    it 'rejects a manifest with produced_at_ms in the future (clock skew)' do
      # A produced_at_ms that's AFTER `now` indicates either a CI
      # machine with a wrong clock or a tampered file. Either way
      # we don't trust it.
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest(
          'produced_at_ms' => (now.to_f * 1000).to_i + 10_000,
        ))
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result).to be_nil
      end
    end

    it 'rejects a manifest with non-matching version_name' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest('version_name' => '0.9.0'))
        result = described_class.find_manifest(
          search_root: dir, version_name: '1.2.3', now: now,
        )
        expect(result).to be_nil
      end
    end

    it 'rejects a manifest with non-matching version_code' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest('version_code' => '99'))
        result = described_class.find_manifest(
          search_root: dir, version_code: '42', now: now,
        )
        expect(result).to be_nil
      end
    end

    it 'accepts the manifest when both version_name and version_code match' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest)
        result = described_class.find_manifest(
          search_root: dir,
          version_name: '1.2.3',
          version_code: '42',
          now: now,
        )
        expect(result).not_to be_nil
      end
    end

    it 'tolerates Integer version_code from the caller' do
      # Real fastlane SharedValues sometimes round-trip versionCode
      # as Integer. The manifest stores String. Cross-type compare
      # MUST work.
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest)
        result = described_class.find_manifest(
          search_root: dir, version_code: 42, now: now,
        )
        expect(result).not_to be_nil
      end
    end

    it 'skips manifests with unknown schema_version' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, manifest('schema_version' => 99))
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result).to be_nil
      end
    end

    it 'skips manifests that are not valid JSON without raising' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, '{ this is not JSON }')
        expect {
          described_class.find_manifest(search_root: dir, now: now)
        }.not_to raise_error
      end
    end

    it 'skips manifests whose top level is not an object' do
      Dir.mktmpdir do |dir|
        write_manifest(dir, '["array", "not", "object"]')
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result).to be_nil
      end
    end

    it 'falls back to the next-newest manifest when the newest is invalid' do
      # Pin behaviour: a malformed newest manifest doesn't lock out
      # an older-but-valid one. Useful when CI clobbers a manifest
      # mid-write and a previous good manifest still describes the
      # right build.
      Dir.mktmpdir do |dir|
        bad = write_manifest(dir, '{not json',
                             'app/build/intermediates/bugsee/release')
        good = write_manifest(dir, manifest('producer' => 'good'),
                              'app/build/intermediates/bugsee/debug')
        FileUtils.touch(bad,  mtime: now)
        FileUtils.touch(good, mtime: now - 30)
        result = described_class.find_manifest(search_root: dir, now: now)
        expect(result['producer']).to eq('good')
      end
    end
  end

  # ──────────────────────────────────────────────────────────────
  # handled_by_other? — per-action skip predicate
  # ──────────────────────────────────────────────────────────────
  describe '.handled_by_other?' do
    it 'returns false when manifest is nil' do
      # The most common case in practice: no other producer ran,
      # so fastlane does the work itself.
      expect(described_class.handled_by_other?(nil, 'mapping_upload')).to be false
    end

    it 'returns true when actions[action] is true' do
      expect(described_class.handled_by_other?(manifest, 'mapping_upload'))
        .to be true
    end

    it 'returns false when actions[action] is false' do
      # `false` in the manifest means "the producer was configured
      # for this build to NOT do it" — fastlane SHOULD do it. Pin
      # this — a regression that treated false-as-truthy would
      # cause fastlane to skip work the user explicitly wants.
      expect(described_class.handled_by_other?(manifest, 'dsym_upload'))
        .to be false
    end

    it 'returns false when actions[action] is absent' do
      # Forward-compatibility: a future producer may emit a
      # manifest with the `actions` key listing only what it knows
      # about. An unknown action key is treated as "not handled".
      expect(described_class.handled_by_other?(manifest, 'novel_action'))
        .to be false
    end

    it 'returns false when actions key is missing entirely' do
      expect(described_class.handled_by_other?({}, 'mapping_upload'))
        .to be false
    end

    it 'returns false when actions is not a Hash' do
      # Defensive: a malformed manifest that passed safe_parse
      # (schema_version OK, top-level Hash) but has actions: [].
      expect(described_class.handled_by_other?(
        { 'actions' => ['mapping_upload'] }, 'mapping_upload'
      )).to be false
    end

    it 'treats truthy non-true values as NOT handled (strict equality)' do
      # The wire contract says boolean. A 1 / "true" / "yes" must
      # NOT be honored as true — that would invite producer-side
      # type bugs. Pin strict-Boolean comparison.
      expect(described_class.handled_by_other?(
        { 'actions' => { 'mapping_upload' => 1 } }, 'mapping_upload'
      )).to be false
      expect(described_class.handled_by_other?(
        { 'actions' => { 'mapping_upload' => 'true' } }, 'mapping_upload'
      )).to be false
    end
  end

  # ──────────────────────────────────────────────────────────────
  # skip_message — log line for the action to surface
  # ──────────────────────────────────────────────────────────────
  describe '.skip_message' do
    it 'returns a message naming the producer when handled' do
      msg = described_class.skip_message(manifest, 'mapping_upload')
      expect(msg).to include('skipping mapping_upload')
      expect(msg).to include('bugsee-android-gradle-plugin')
      expect(msg).to include('1.2.3')
    end

    it 'returns nil when the action is NOT handled' do
      # If fastlane is doing the work itself, there's nothing
      # interesting to log — return nil so the caller can guard
      # cleanly.
      expect(described_class.skip_message(manifest, 'dsym_upload'))
        .to be_nil
    end

    it 'returns nil when the manifest is nil' do
      expect(described_class.skip_message(nil, 'mapping_upload')).to be_nil
    end

    it 'tolerates missing producer_version' do
      m = manifest
      m.delete('producer_version')
      msg = described_class.skip_message(m, 'mapping_upload')
      expect(msg).not_to be_nil
      # Should not include a stray empty parens or "()".
      expect(msg).not_to include('()')
    end
  end

  # ──────────────────────────────────────────────────────────────
  # ACTION_KEYS — cross-repo contract pin
  # ──────────────────────────────────────────────────────────────
  describe 'ACTION_KEYS' do
    # The Gradle plugin and the iOS SDK BugseeAgent MUST emit
    # `actions` keys from this set. Pinning the list here gives a
    # mechanical regression target — anyone adding a new feature
    # must update this constant AND the producer code in lockstep.
    it 'matches the documented producer contract' do
      expect(described_class::ACTION_KEYS).to eq(%w[
        mapping_upload
        dsym_upload
        deps_collection
        timings
        size_analysis
        artifact_upload
      ])
    end
  end

  describe 'STALENESS_WINDOW_S' do
    # 1 hour. Documented in the helper KDoc. A regression that
    # shrank it to e.g. 60 (seconds) would cause every CI run to
    # ignore the manifest the previous task wrote 90 seconds ago.
    it 'is one hour' do
      expect(described_class::STALENESS_WINDOW_S).to eq(60 * 60)
    end
  end
end

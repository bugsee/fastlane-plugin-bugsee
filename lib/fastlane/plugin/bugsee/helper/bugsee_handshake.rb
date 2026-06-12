# frozen_string_literal: true

require 'json'

module Fastlane
  module Bugsee
    # Cross-producer build-action handshake.
    #
    # When more than one Bugsee producer can run on a build —
    # typically the Bugsee Android Gradle plugin OR the Bugsee iOS
    # SDK's `tools.bundle/BugseeAgent` build phase running ALONGSIDE
    # this fastlane plugin — we want only ONE producer to handle
    # each action (mapping upload, dSYM upload, deps collection,
    # build timings, etc.). Otherwise the same payload reaches the
    # server twice; the server dedupes by hash so it's not
    # *broken*, but the lane log gets noisy and the customer pays
    # for redundant bandwidth.
    #
    # Each producer that runs writes a small JSON manifest
    # declaring which actions it handled for THIS build. The
    # fastlane plugin reads the most recent matching manifest
    # before each action and skips work the other producer already
    # did.
    #
    # ## Manifest locations
    #
    # - **Bugsee Android Gradle plugin** writes
    #   `<project>/**/build/intermediates/bugsee/<variant>/build-actions.json`
    #   from its existing variant tasks (same parent dir as
    #   `build-id.txt`).
    # - **Bugsee iOS SDK `tools.bundle/BugseeAgent`** writes
    #   `<srcroot>/build/bugsee/build-actions.json` from its
    #   post-build Xcode build phase.
    #
    # Both formats share the JSON shape below.
    #
    # ## Manifest schema (version 1)
    #
    #     {
    #       "schema_version":   1,
    #       "producer":         "bugsee-android-gradle-plugin"
    #                           | "bugsee-ios-sdk-tools-bundle",
    #       "producer_version": "1.2.3",
    #       "build_id":         "<BUILD_UUID>",
    #       "produced_at_ms":   1700000000000,
    #       "version_name":     "1.2.3",
    #       "version_code":     "42",
    #       "actions": {
    #         "mapping_upload":   true | false,
    #         "dsym_upload":      true | false,
    #         "deps_collection":  true | false,
    #         "timings":          true | false,
    #         "size_analysis":    true | false
    #       }
    #     }
    #
    # A `true` value means the named producer ran that action for
    # this build (whether it succeeded or failed — retrying isn't
    # fastlane's concern). A `false` or absent value means the
    # producer was either not configured to handle it or skipped
    # for this variant. Unknown keys are reserved for forward
    # compatibility; readers must ignore them.
    #
    # ## Staleness window
    #
    # A manifest is considered valid only when its `produced_at_ms`
    # is within {STALENESS_WINDOW_S} seconds of `Time.now`. A stale
    # manifest from last week's build is treated as absent; fastlane
    # does the work itself.
    #
    # ## Build-identity match
    #
    # If the action passes explicit `:version` / `:build`
    # ConfigItems, those are checked against the manifest's
    # `version_name` / `version_code`. A mismatch indicates the
    # manifest is from a different build (developer's previous
    # archive, CI's previous run, etc.) and is treated as absent.
    # When the action doesn't pass them, build-identity matching
    # is skipped and the manifest is honored as long as it's not
    # stale.
    #
    # ## Override
    #
    # Every action that uses this helper takes a `:force` ConfigItem
    # that short-circuits the handshake check. Useful for one-off
    # re-uploads, debugging, or cases where the user wants fastlane
    # to do the work regardless of what the other producer claims.
    module Handshake
      # Seconds — manifests older than this are treated as absent.
      # A normal CI run is rare to last more than an hour; a local
      # dev rerun the next morning genuinely is a different build.
      STALENESS_WINDOW_S = 60 * 60

      # The canonical action keys the manifest's `actions` block
      # uses. Producers SHOULD emit only these — unknown keys are
      # ignored by readers but may be reserved for future features.
      ACTION_KEYS = %w[
        mapping_upload
        dsym_upload
        deps_collection
        timings
        size_analysis
      ].freeze

      # Filesystem glob patterns for locating manifests. The list
      # is ordered so that a project laid out for both platforms
      # at once (e.g. a cross-platform fastlane lane) picks the
      # most-likely-relevant manifest first.
      MANIFEST_GLOBS = [
        # Android Gradle plugin output. The `**/` covers multi-
        # module projects where the `app/` module nests below a
        # top-level checkout. The intermediates dir is per-variant
        # so a project shipping multiple variants will produce
        # multiple manifests; the staleness + identity check
        # narrows to the right one.
        "**/build/intermediates/bugsee/*/build-actions.json",
        # iOS SDK BugseeAgent output. Single file per project — no
        # variant slot since iOS targets multiplex through one
        # build phase.
        "**/build/bugsee/build-actions.json",
      ].freeze

      # Locate the manifest most likely to describe THIS build.
      #
      # @param search_root [String, nil] directory to search under.
      #   Defaults to current working directory (most fastlane
      #   lanes cd to the project root before invoking actions).
      # @param version_name [String, nil] expected android:versionName
      #   / iOS CFBundleShortVersionString. When given, the manifest
      #   must match — used to reject stale manifests from a
      #   previous build at a different version.
      # @param version_code [String, Integer, nil] expected
      #   android:versionCode / iOS CFBundleVersion. Same role.
      # @param now [Time] for tests — production callers omit it.
      # @return [Hash, nil] the parsed manifest, or `nil` when no
      #   valid manifest was found.
      def self.find_manifest(search_root: Dir.pwd,
                              version_name: nil,
                              version_code: nil,
                              now: Time.now)
        candidates = MANIFEST_GLOBS.flat_map do |glob|
          Dir.glob(File.join(search_root, glob))
        end
        # Sort newest-first by file mtime; then validity-check.
        # First valid wins.
        candidates
          .sort_by { |p| -File.mtime(p).to_f }
          .each do |path|
          parsed = safe_parse(path)
          next unless parsed
          next unless valid_for_build?(
            parsed,
            version_name: version_name,
            version_code: version_code,
            now: now,
          )
          return parsed
        end
        nil
      end

      # Per-action skip check.
      #
      # @param manifest [Hash, nil] the manifest returned by
      #   {find_manifest}, or `nil` when none was found.
      # @param action [String] one of {ACTION_KEYS}.
      # @return [Boolean] true when ANOTHER producer ran this
      #   action and fastlane should skip; false otherwise
      #   (manifest absent, action not listed, action listed false).
      def self.handled_by_other?(manifest, action)
        return false if manifest.nil?
        actions = manifest['actions']
        return false unless actions.is_a?(Hash)
        actions[action] == true
      end

      # Convenience: when {handled_by_other?} is true, build a
      # log line the action can surface so the user understands
      # why fastlane is skipping. Returns nil otherwise.
      def self.skip_message(manifest, action)
        return nil unless handled_by_other?(manifest, action)
        producer = manifest['producer'] || 'another Bugsee producer'
        version = manifest['producer_version']
        version_str = version ? " (#{version})" : ''
        "Bugsee: skipping #{action} — already handled by #{producer}#{version_str} for this build"
      end

      # @api private
      # Parse + structurally validate a manifest file. Returns the
      # parsed Hash on success, nil on any error. Errors are
      # SILENT — a malformed manifest must not break the fastlane
      # lane.
      def self.safe_parse(path)
        return nil unless File.file?(path)
        raw = File.read(path)
        return nil if raw.nil? || raw.empty?
        parsed = JSON.parse(raw)
        return nil unless parsed.is_a?(Hash)
        return nil unless parsed['schema_version'] == 1
        parsed
      rescue JSON::ParserError, Errno::ENOENT, Errno::EACCES
        nil
      end

      # @api private
      # Apply the staleness window + build-identity match.
      def self.valid_for_build?(manifest, version_name:, version_code:, now:)
        produced_at_ms = manifest['produced_at_ms']
        return false unless produced_at_ms.is_a?(Numeric)
        age_s = now.to_f - (produced_at_ms.to_f / 1000.0)
        # Negative age (manifest from the future — clock skew or
        # tampered file) is rejected. Excess positive age too.
        return false if age_s < 0
        return false if age_s > STALENESS_WINDOW_S

        if version_name && !version_name.to_s.empty?
          return false unless manifest['version_name'].to_s == version_name.to_s
        end
        if version_code && !version_code.to_s.empty?
          return false unless manifest['version_code'].to_s == version_code.to_s
        end
        true
      end
    end
  end
end

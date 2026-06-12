# frozen_string_literal: true

require 'digest'

module Fastlane
  module Bugsee
    # Cross-language `UUID.nameUUIDFromBytes` equivalent.
    #
    # The Bugsee Android SDK (Java) computes synthesized BUILD_UUIDs
    # via {@code java.util.UUID.nameUUIDFromBytes(bytes)}. Per RFC
    # 4122 / the Java spec, that's MD5 over the input bytes with two
    # bit twiddles:
    #
    #   - byte 6, upper nibble: clear, then set to 0011 (UUID
    #     version 3 — name-based, MD5)
    #   - byte 8, upper two bits: clear, then set to 10 (variant
    #     RFC 4122)
    #
    # The Bugsee Rust CLI's `bugsee-cli` replicates the same
    # primitive for ProGuard-mapping-content-derived UUIDs (the
    # `bugsee_cli_proguard_uuid_compat` parity is already proven and
    # locked in). This Ruby implementation closes the loop for the
    # fastlane plugin's mapping-upload action: when no explicit
    # `:uuid` is passed and no Gradle-plugin-emitted `build-uuid.txt`
    # is found, the action synthesizes a UUID Ruby-side from
    # `(app_token, version_name, version_code)` and uploads the
    # mapping with that UUID; the SDK (7.0.0-beta13+) computes the
    # same bytes at runtime via the same primitive and the server
    # matches.
    #
    # This module has ZERO fastlane dependencies — the unit tests can
    # require it directly without loading the whole fastlane runtime.
    module Uuid
      # ASCII Unit Separator (0x1F). Same byte the SDK uses to join
      # the synthesis inputs (see
      # `BugseeEnvironment.BUILD_ID_SYNTHESIS_SEPARATOR`). Guaranteed
      # never to appear in any of the three inputs (app token is
      # ASCII hex, versionName is dotted, versionCode is digits).
      SEPARATOR = ""

      # Ruby equivalent of Java's `UUID.nameUUIDFromBytes(bytes)`.
      # Takes a String (treated as UTF-8 bytes) or a Binary String,
      # returns the canonical 8-4-4-4-12 lowercase UUID string.
      #
      # NOT a cryptographic primitive. MD5 is deprecated for security
      # uses but the Java spec mandates it for `nameUUIDFromBytes`,
      # and we MUST stay byte-for-byte compatible with the SDK side.
      def self.name_uuid_from_bytes(input)
        # Force UTF-8 encoding before digest so that a future caller
        # passing an ASCII-8BIT (binary) string doesn't accidentally
        # round-trip through a different code path. Digest::MD5 sees
        # bytes either way, but normalizing upstream removes one
        # category of "why don't these match" puzzles.
        bytes = input.dup.force_encoding(Encoding::UTF_8).bytes
        md5 = Digest::MD5.digest(bytes.pack('C*'))
        raw = md5.bytes

        # UUID version (4 high bits of byte 6) = 3 (name-based, MD5).
        raw[6] = (raw[6] & 0x0f) | 0x30
        # UUID variant (2 high bits of byte 8) = RFC 4122 (binary 10).
        raw[8] = (raw[8] & 0x3f) | 0x80

        hex = raw.map { |b| b.to_s(16).rjust(2, '0') }.join
        "#{hex[0, 8]}-#{hex[8, 4]}-#{hex[12, 4]}-#{hex[16, 4]}-#{hex[20, 12]}"
      end

      # Synthesize a BUILD_UUID matching what the Bugsee Android SDK
      # 7.0.0-beta13+ computes at runtime when neither the asset
      # channel nor the manifest meta-data channel populated.
      #
      # Formula (must stay byte-for-byte aligned with the SDK side —
      # see {@code BugseeEnvironment.synthesizeBuildIdFromVersion}):
      #
      #     name_uuid_from_bytes(
      #       app_token + 0x1F + version_name + 0x1F + version_code
      #     )
      #
      # @param app_token   [String] the Bugsee application token
      #                    (mandatory — disambiguates apps within
      #                    the same Bugsee organisation).
      # @param version_name [String, nil] android:versionName from
      #                    the merged manifest. {@code nil}
      #                    coerces to empty string (matches the
      #                    SDK side's null-versionName coercion).
      # @param version_code [Integer, String] android:versionCode.
      #                    Coerced to base-10 string. The SDK uses
      #                    {@code getLongVersionCode().toString()};
      #                    we accept either an Integer or the
      #                    pre-stringified form so the fastlane
      #                    ConfigItem can take either.
      def self.synthesize_build_uuid(app_token, version_name, version_code)
        raise ArgumentError, "app_token is required" if app_token.nil? || app_token.to_s.empty?

        version_name_str = version_name.nil? ? "" : version_name.to_s
        version_code_str = version_code.nil? ? "0" : version_code.to_s
        input = "#{app_token}#{SEPARATOR}#{version_name_str}#{SEPARATOR}#{version_code_str}"
        name_uuid_from_bytes(input)
      end
    end
  end
end

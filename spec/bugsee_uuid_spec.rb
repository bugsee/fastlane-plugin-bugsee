# frozen_string_literal: true

# Unit tests for the Ruby-side nameUUIDFromBytes shim and the
# (app_token, version_name, version_code) synthesis formula.
#
# The CRITICAL test in this file is `pinned_reference_vector`: it
# verifies that the Ruby helper produces the EXACT SAME UUID as the
# Bugsee Android SDK's runtime synthesis and the bugsee-cli's
# mapping-content compute, for the documented reference input. The
# SDK side has the matching test in
# BugseeEnvironmentBuildIdReaderTest.channel3_synthesisFormula_pinnedReferenceVector
# — the two pin the same expected UUID so a drift on either side
# fails CI on that side immediately.

require 'fastlane/plugin/bugsee/helper/bugsee_uuid'

describe Fastlane::Bugsee::Uuid do
  describe '.name_uuid_from_bytes' do
    it 'matches Java UUID.nameUUIDFromBytes for the canonical empty-string case' do
      # Java spec: UUID.nameUUIDFromBytes(new byte[0]) for empty
      # input produces the MD5 hash with version-3 bits set on byte
      # 6 (b2 → 32) and RFC-4122 variant bits set on byte 8 (e9 →
      # a9). The raw MD5 of "" is d41d8cd98f00b204e9800998ecf8427e;
      # post-twiddling it becomes the value below. A regression in
      # the MD5 wiring OR in either bit-twiddle would change this.
      expect(described_class.name_uuid_from_bytes(""))
        .to eq("d41d8cd9-8f00-3204-a980-0998ecf8427e")
    end

    it 'sets the UUID-version-3 nibble' do
      # Per RFC 4122 + Java spec: byte 6 high nibble must be 0x3 for
      # name-based MD5 UUIDs. A mutation that dropped the version
      # twiddle would surface here as a different high nibble in
      # the 14th hex char.
      out = described_class.name_uuid_from_bytes("anything")
      version_nibble = out[14]
      expect(version_nibble).to eq("3"),
        "expected version-3 UUID (RFC 4122 / MD5), got version-#{version_nibble} UUID '#{out}'"
    end

    it 'sets the RFC-4122 variant bits' do
      # Per RFC 4122 + Java spec: byte 8 high 2 bits must be 10
      # binary, so the first hex digit of byte 8 (UUID position 19)
      # is one of 8 / 9 / a / b.
      out = described_class.name_uuid_from_bytes("anything")
      variant_nibble = out[19]
      expect(%w[8 9 a b]).to include(variant_nibble),
        "expected RFC-4122 variant (high bits 10), got '#{variant_nibble}' in UUID '#{out}'"
    end

    it 'produces lowercase hex with the 8-4-4-4-12 layout' do
      out = described_class.name_uuid_from_bytes("anything")
      expect(out).to match(/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/)
    end

    it 'is deterministic — same input always produces the same UUID' do
      a = described_class.name_uuid_from_bytes("repeat-me")
      b = described_class.name_uuid_from_bytes("repeat-me")
      expect(a).to eq(b)
    end

    it 'is sensitive to a single-byte change' do
      a = described_class.name_uuid_from_bytes("abc")
      b = described_class.name_uuid_from_bytes("abd")
      expect(a).not_to eq(b)
    end

    it 'normalises ASCII-8BIT input to UTF-8 bytes' do
      # `Café` UTF-8 = 43 61 66 c3 a9 (5 bytes). The helper calls
      # `force_encoding(Encoding::UTF_8)` on its input — that
      # reinterprets the encoding tag but the raw bytes don't
      # change, so a caller handing in an ASCII-8BIT (binary) buffer
      # whose bytes happen to be valid UTF-8 must produce the SAME
      # UUID as the same bytes tagged UTF-8. Drift in the
      # normalisation would surface here as a divergent UUID.
      utf8_in   = "Café".dup.force_encoding(Encoding::UTF_8)
      binary_in = "Café".dup.force_encoding(Encoding::ASCII_8BIT)
      expect(utf8_in.bytes).to eq(binary_in.bytes),
        "preconditions: same raw bytes, different encoding tag"
      expect(described_class.name_uuid_from_bytes(utf8_in))
        .to eq(described_class.name_uuid_from_bytes(binary_in))
    end
  end

  describe '.synthesize_build_uuid' do
    context 'pinned reference vector' do
      # MUST stay byte-for-byte identical with the SDK's pin at
      # BugseeEnvironmentBuildIdReaderTest.channel3_synthesisFormula_pinnedReferenceVector
      # and the bugsee-cli's compute for the same input bytes.
      it 'computes the documented cross-platform reference UUID' do
        # nameUUIDFromBytes("tok" + 0x1F + "1.2.3" + 0x1F + "42")
        # MUST equal this exact string. Drift in MD5, separator,
        # encoding, version bits, OR variant bits all change this.
        expect(described_class.synthesize_build_uuid("tok", "1.2.3", 42))
          .to eq("b27501e9-5a6a-3227-9f7d-f4696b48739c")
      end
    end

    it 'accepts integer or string version code' do
      from_int = described_class.synthesize_build_uuid("tok", "1.0", 42)
      from_str = described_class.synthesize_build_uuid("tok", "1.0", "42")
      expect(from_int).to eq(from_str)
    end

    it 'coerces nil version_name to empty string' do
      # SDK Channel 3 contract: a null versionName from PackageInfo
      # is coerced to "" in the digest input. The Ruby side MUST
      # do the same so a fastlane invocation that omits :version
      # produces the same UUID the SDK will compute at runtime.
      from_nil    = described_class.synthesize_build_uuid("tok", nil, 42)
      from_empty  = described_class.synthesize_build_uuid("tok", "", 42)
      expect(from_nil).to eq(from_empty)
    end

    it 'coerces nil version_code to "0"' do
      from_nil   = described_class.synthesize_build_uuid("tok", "1.0", nil)
      from_zero  = described_class.synthesize_build_uuid("tok", "1.0", "0")
      expect(from_nil).to eq(from_zero)
    end

    it 'raises when app_token is nil or empty' do
      expect { described_class.synthesize_build_uuid(nil, "1.0", 42) }
        .to raise_error(ArgumentError, /app_token is required/)
      expect { described_class.synthesize_build_uuid("", "1.0", 42) }
        .to raise_error(ArgumentError, /app_token is required/)
    end

    it 'differentiates apps in the same org with same version' do
      # The reason the token is in the digest. Two apps in the same
      # Bugsee organisation, same versionName + versionCode → MUST
      # produce different UUIDs so a mapping uploaded for app A
      # doesn't resolve crashes from app B.
      a = described_class.synthesize_build_uuid("token-app-A", "1.0", 42)
      b = described_class.synthesize_build_uuid("token-app-B", "1.0", 42)
      expect(a).not_to eq(b)
    end

    it 'differentiates two builds of the same app at different versions' do
      a = described_class.synthesize_build_uuid("tok", "1.0", 1)
      b = described_class.synthesize_build_uuid("tok", "1.1", 1)
      expect(a).not_to eq(b)
    end

    it 'differentiates two builds at the same version, different code' do
      # Realistic: same versionName ("1.0"), bumped versionCode.
      # The SDK distinguishes these as separate builds, so the
      # synthesis must too.
      a = described_class.synthesize_build_uuid("tok", "1.0", 1)
      b = described_class.synthesize_build_uuid("tok", "1.0", 2)
      expect(a).not_to eq(b)
    end

    it 'uses ASCII Unit Separator (0x1F) — pinned' do
      # A drift to ":" / "|" / "\n" would change all UUIDs and
      # break parity with the SDK side. The constant is exposed via
      # SEPARATOR; this test pins its byte value.
      expect(described_class::SEPARATOR.bytes).to eq([0x1F]),
        "SEPARATOR must be the single byte 0x1F (ASCII Unit Separator); " \
        "got bytes #{described_class::SEPARATOR.bytes.inspect}"
    end
  end
end

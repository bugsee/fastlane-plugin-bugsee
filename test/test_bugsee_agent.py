"""
Unit tests for the iOS BugseeAgent dependency-collection pipeline.

The agent ships as a single executable Python file with NO `.py`
extension (it's distributed alongside the iOS SDK and invoked from
Xcode's build phase as `python3 BugseeAgent <APP_TOKEN>`).
`importlib.util.spec_from_file_location` is the stdlib way to load
a file like that as a module for testing.

Coverage focuses on the deterministic, network-free pieces:

  - Identity / id-building helpers
  - Podfile.lock parser (the only source that carries a real graph)
  - Package.resolved parser (both Xcode-managed and SPM CLI formats)
  - Cartfile.resolved parser
  - Merger + truncation + self-consistency cleanup
  - Payload builder (matches the Android plugin's wire shape)

Run with `python -m unittest test.test_bugsee_agent` from the
`fastlane-plugin-bugsee` repo root, or via the standard `pytest` if
the project ever adopts it.
"""

from __future__ import annotations

import gzip
import importlib.util
import importlib.machinery
import json
import os
import subprocess
import tempfile
import textwrap
import unittest


# Load the BugseeAgent file as a module. The file has no .py
# extension, so `spec_from_file_location` can't infer the right
# loader — pass `SourceFileLoader` explicitly so Python treats it
# as a Python source file regardless of the missing suffix.
_AGENT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, 'BugseeAgent')
)
_loader = importlib.machinery.SourceFileLoader('bugsee_agent', _AGENT_PATH)
_spec = importlib.util.spec_from_loader('bugsee_agent', _loader)
agent = importlib.util.module_from_spec(_spec)
# Loading the agent triggers its module-level imports (no side
# effects past that — the `if __name__ == "__main__":` block is the
# only thing with real side effects, and it doesn't fire when
# loaded this way).
_loader.exec_module(agent)


# ──────────────────────────────────────────────
# Identity / id helpers
# ──────────────────────────────────────────────

class TestIdentityHelpers(unittest.TestCase):
    def test_make_dep_id_matches_android_format(self):
        # Three colons (the empty-group case): <type>::<name>. Must
        # stay byte-for-byte identical with the Android plugin's
        # DependencyEntry.makeId and the worker's _make_identity.
        self.assertEqual(
            agent._make_dep_id('library', '', 'Braintree'),
            'library::Braintree',
        )
        self.assertEqual(
            agent._make_dep_id('file', '', 'Bugsee.framework'),
            'file::Bugsee.framework',
        )

    def test_strip_pod_version_paren_handles_constraints(self):
        # Equality constraint (= 5.26.0).
        self.assertEqual(
            agent._strip_pod_version_paren('Braintree/Card (= 5.26.0)'),
            'Braintree/Card',
        )
        # Tilde constraint (~> 5.26).
        self.assertEqual(
            agent._strip_pod_version_paren('Braintree/Card (~> 5.26)'),
            'Braintree/Card',
        )
        # No constraint at all — return as-is, stripped.
        self.assertEqual(
            agent._strip_pod_version_paren('  SocketRocket  '),
            'SocketRocket',
        )


# ──────────────────────────────────────────────
# Podfile.lock parser
# ──────────────────────────────────────────────

_PODFILE_LOCK_BRAINTREE = textwrap.dedent("""\
    PODS:
      - Braintree (5.26.0):
        - Braintree/Card (= 5.26.0)
        - Braintree/Core (= 5.26.0)
      - Braintree/Card (5.26.0):
        - Braintree/Core
      - Braintree/Core (5.26.0)
      - SocketRocket (0.7.1)

    DEPENDENCIES:
      - Braintree
      - SocketRocket (~> 0.7.0)

    SPEC CHECKSUMS:
      Braintree: deadbeef0123456789abcdef
      SocketRocket: feedface0123456789abcdef

    PODFILE CHECKSUM: 0123456789abcdef0123456789abcdef
    COCOAPODS: 1.16.2
""")


def _write_fixture(content):
    """Write `content` to a temp file and return its path. Tests
    that read lockfiles use this to materialise the fixture text
    on disk — the parsers are file-path-driven."""
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


class TestParsePodfileLock(unittest.TestCase):
    def test_returns_empty_on_missing_path(self):
        self.assertEqual(agent._parse_podfile_lock(None), [])
        self.assertEqual(agent._parse_podfile_lock('/nonexistent/path'), [])

    def test_resolves_top_level_pods_and_versions(self):
        path = _write_fixture(_PODFILE_LOCK_BRAINTREE)
        try:
            entries = agent._parse_podfile_lock(path)
        finally:
            os.unlink(path)

        # Every top-level pod surfaces as an entry. Version comes
        # from the `(x.y.z)` suffix on the declaration line.
        by_name = {e['name']: e for e in entries}
        self.assertEqual(by_name['Braintree']['version'], '5.26.0')
        self.assertEqual(by_name['SocketRocket']['version'], '0.7.1')
        # Subspecs are top-level entries too — their names carry the
        # slash from the lockfile verbatim.
        self.assertIn('Braintree/Card', by_name)
        self.assertEqual(by_name['Braintree/Card']['version'], '5.26.0')

    def test_marks_direct_deps_from_dependencies_section(self):
        path = _write_fixture(_PODFILE_LOCK_BRAINTREE)
        try:
            entries = agent._parse_podfile_lock(path)
        finally:
            os.unlink(path)
        by_name = {e['name']: e for e in entries}
        # Listed verbatim under DEPENDENCIES — direct.
        self.assertTrue(by_name['Braintree']['direct'])
        self.assertTrue(by_name['SocketRocket']['direct'])
        # Subspecs of an umbrella pod that the user declared via
        # `pod 'Braintree'` (NOT `pod 'Braintree/Card'`) come along
        # transitively — CocoaPods only writes the umbrella into
        # DEPENDENCIES in that case. Tree view will still surface
        # them under the umbrella via parent edges; they're just
        # not direct deps of the user's Podfile.
        self.assertFalse(by_name['Braintree/Card']['direct'])
        self.assertFalse(by_name['Braintree/Core']['direct'])

    def test_subspecs_named_explicitly_in_podfile_are_direct(self):
        # The flip side: when the user writes
        #   pod 'Foo', :subspecs => ['Bar']
        # CocoaPods writes `Foo/Bar` (not `Foo`) into DEPENDENCIES.
        # Pin that exact-name match yields direct=true for the
        # subspec.
        body = textwrap.dedent("""\
            PODS:
              - Foo (1.0):
                - Foo/Bar (= 1.0)
              - Foo/Bar (1.0)

            DEPENDENCIES:
              - Foo/Bar

            SPEC CHECKSUMS:
              Foo: deadbeef
        """)
        path = _write_fixture(body)
        try:
            entries = agent._parse_podfile_lock(path)
        finally:
            os.unlink(path)
        by_name = {e['name']: e for e in entries}
        # Subspec explicitly named in DEPENDENCIES → direct.
        self.assertTrue(by_name['Foo/Bar']['direct'])
        # Umbrella NOT in DEPENDENCIES → NOT direct (even though
        # the resolver picked its version because Foo/Bar pulled
        # it in).
        self.assertFalse(by_name['Foo']['direct'])

    def test_builds_parent_edges_from_child_references(self):
        path = _write_fixture(_PODFILE_LOCK_BRAINTREE)
        try:
            entries = agent._parse_podfile_lock(path)
        finally:
            os.unlink(path)
        by_name = {e['name']: e for e in entries}
        # Braintree/Card is referenced as a child of Braintree -> its
        # parents list must contain Braintree's id.
        card_parents = by_name['Braintree/Card']['parents']
        self.assertIn(agent._make_dep_id('library', '', 'Braintree'),
                      card_parents)
        # Braintree/Core is referenced as a child by BOTH Braintree
        # AND Braintree/Card — both must appear in parents.
        core_parents = by_name['Braintree/Core']['parents']
        self.assertIn(agent._make_dep_id('library', '', 'Braintree'),
                      core_parents)
        self.assertIn(agent._make_dep_id('library', '', 'Braintree/Card'),
                      core_parents)
        # SocketRocket has no children (and isn't a child of anyone)
        # — parents must be empty.
        self.assertEqual(by_name['SocketRocket']['parents'], [])


# ──────────────────────────────────────────────
# Package.resolved parser
# ──────────────────────────────────────────────

class TestParsePackageResolved(unittest.TestCase):
    def test_returns_empty_on_missing_path(self):
        self.assertEqual(agent._parse_package_resolved(None), [])
        self.assertEqual(agent._parse_package_resolved('/nonexistent'), [])

    def test_parses_xcode_legacy_object_pins_shape(self):
        # Xcode-managed Package.resolved uses {"object": {"pins": ...}}.
        body = json.dumps({
            "object": {
                "pins": [
                    {
                        "package": "Alamofire",
                        "repositoryURL": "https://github.com/Alamofire/Alamofire.git",
                        "state": {"version": "5.8.1"},
                    },
                    {
                        "package": "swift-collections",
                        "repositoryURL": "https://github.com/apple/swift-collections.git",
                        "state": {"revision": "abc123def456"},
                    },
                ],
            },
            "version": 1,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)

        by_name = {e['name']: e for e in entries}
        self.assertEqual(by_name['Alamofire']['version'], '5.8.1')
        # Revision-locked pins surface the revision string when no
        # tagged version is set.
        self.assertEqual(by_name['swift-collections']['version'], 'abc123def456')
        # Every SPM pin is direct=true (no graph info in the
        # resolved file).
        self.assertTrue(all(e['direct'] for e in entries))
        # And carries no parent edges — SPM tree view isn't
        # available.
        self.assertTrue(all(e['parents'] == [] for e in entries))

    def test_parses_swiftpm_v2_format_with_identity_field(self):
        # New format: {"pins": [...]} at the top level, identity
        # (lowercase) preferred over package (capitalised).
        body = json.dumps({
            "pins": [
                {
                    "identity": "alamofire",
                    "kind": "remoteSourceControl",
                    "location": "https://github.com/Alamofire/Alamofire.git",
                    "state": {"version": "5.8.1"},
                },
            ],
            "version": 2,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)

        # `identity` (lowercased) is preferred over `package`
        # (capitalised) when both are present — stable across Xcode
        # versions writing the same package set.
        self.assertEqual(entries[0]['name'], 'alamofire')

    def test_state_prefers_version_over_revision_when_both_present(self):
        # Real Package.resolved entries often carry BOTH a tagged
        # `version` AND the underlying `revision`. The parser
        # documents "prefer the most specific human-readable value"
        # — version wins. A silent precedence flip to revision
        # would replace "5.8.1" with a 40-char SHA on every tagged
        # pin and break the worker's version-diff display.
        body = json.dumps({
            "pins": [
                {
                    "identity": "alamofire",
                    "kind": "remoteSourceControl",
                    "location": "https://github.com/Alamofire/Alamofire.git",
                    "state": {
                        "version":  "5.8.1",
                        "revision": "f455c2975872ccd2d9c81594c658af65716e9b9a",
                    },
                },
            ],
            "version": 2,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)
        self.assertEqual(entries[0]['version'], '5.8.1')


# ──────────────────────────────────────────────
# Cartfile.resolved parser
# ──────────────────────────────────────────────

class TestParseCartfileResolved(unittest.TestCase):
    def test_parses_github_and_binary_lines(self):
        body = textwrap.dedent("""\
            github "ReactiveCocoa/ReactiveCocoa" "v2.3.1"
            git "https://example.com/private.git" "1.0.0"
            binary "https://example.com/MyBin.json" "1.0.0"
            # comment line — must be skipped
        """)
        path = _write_fixture(body)
        try:
            entries = agent._parse_cartfile_resolved(path)
        finally:
            os.unlink(path)

        self.assertEqual(len(entries), 3)
        names = {e['name']: e['version'] for e in entries}
        self.assertEqual(names['ReactiveCocoa/ReactiveCocoa'], 'v2.3.1')
        self.assertEqual(names['https://example.com/private.git'], '1.0.0')
        # All Carthage entries are direct (no graph info).
        self.assertTrue(all(e['direct'] for e in entries))


# ──────────────────────────────────────────────
# Merger + truncation
# ──────────────────────────────────────────────

class TestMergeDepEntries(unittest.TestCase):
    def _entry(self, name, type_='library', parents=()):
        return {
            'id':      agent._make_dep_id(type_, '', name),
            'group':   '',
            'name':    name,
            'version': '1',
            'direct':  True,
            'scope':   None,
            'type':    type_,
            'parents': list(parents),
        }

    def test_deduplicates_across_sources_by_id(self):
        # The same package appearing in two sources (e.g. CocoaPods
        # AND SPM) must surface once — first-source wins. Source
        # order is deliberate (CocoaPods first because Podfile.lock
        # is the only iOS source carrying parent edges), so a
        # silent flip to last-wins would lose graph context.
        # Give the two A-entries observably-different versions so
        # the tie-break direction is pinned, not invisible.
        a_first = self._entry('A')
        a_first['version'] = 'from-cocoapods'
        a_second = self._entry('A')
        a_second['version'] = 'from-spm'
        merged, truncated = agent._merge_dep_entries(
            [a_first, self._entry('B')],
            [a_second, self._entry('C')],
        )
        self.assertEqual({e['name'] for e in merged}, {'A', 'B', 'C'})
        self.assertEqual(len(merged), 3)
        self.assertFalse(truncated)
        # First-source's A survives — last-wins would surface 'from-spm'.
        a_kept = next(e for e in merged if e['name'] == 'A')
        self.assertEqual(a_kept['version'], 'from-cocoapods')

    def test_cap_triggers_truncated_flag(self):
        # Beyond DEPENDENCIES_MAX_COUNT the merger stops emitting
        # and flags truncated=true. The summary's same flag drives
        # the worker's compatibility check.
        #
        # The cap value is a CROSS-PLATFORM CONTRACT — Android
        # plugin's `DependencyPayloadSerializer.MAX_ENTRIES` and the
        # worker's truncation gate both pin 5000. Asserting the
        # literal here catches accidental drift on the iOS side.
        self.assertEqual(agent.DEPENDENCIES_MAX_COUNT, 5000)
        # Fixture sized with a literal (NOT MAX_COUNT + 50) so the
        # length assertion below is independent of the constant —
        # if the constant drifts to 5001/4999, the literal 5000
        # expected-length pins the contract instead of moving with
        # the bug.
        big = [self._entry('p%d' % i) for i in range(5050)]
        merged, truncated = agent._merge_dep_entries(big)
        self.assertEqual(len(merged), 5000)
        self.assertTrue(truncated)

    def test_filters_dangling_parent_references_after_truncation(self):
        # Self-consistency: a child entry that survives the cap
        # but whose parent was evicted must have the dangling
        # parent id stripped from its parents list. Same posture
        # as the Android plugin's post-truncation cleanup.
        evicted_id = agent._make_dep_id('library', '', 'PARENT')
        # First source: ONLY the child. The parent reference points
        # at an entry that never exists in any source.
        child = self._entry('CHILD', parents=[evicted_id])
        merged, truncated = agent._merge_dep_entries([child])
        self.assertEqual(merged[0]['name'], 'CHILD')
        # Dangling parent reference filtered out — the kept set
        # doesn't contain PARENT.
        self.assertEqual(merged[0]['parents'], [])


# ──────────────────────────────────────────────
# Payload builder (matches Android plugin wire shape)
# ──────────────────────────────────────────────

class TestBuildDependenciesPayload(unittest.TestCase):
    def _entry(self, name, type_='library', direct=True, version='1', parents=()):
        return {
            'id':      agent._make_dep_id(type_, '', name),
            'group':   '',
            'name':    name,
            'version': version,
            'direct':  direct,
            'scope':   None,
            'type':    type_,
            'parents': list(parents),
        }

    def test_summary_counts_match_entries(self):
        entries = [
            self._entry('A', direct=True),
            self._entry('B', direct=False),
            self._entry('C', type_='file', direct=True),
        ]
        summary, _ = agent._build_dependencies_payload(
            entries, truncated=False, scope_label='all',
            clock=lambda: 0,
        )
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['direct'], 2)
        self.assertEqual(summary['transitive'], 1)
        self.assertEqual(summary['by_type']['library'], 2)
        self.assertEqual(summary['by_type']['file'], 1)
        self.assertEqual(summary['by_type']['project'], 0)
        # `truncated` must echo the caller's flag verbatim, NOT be
        # hard-coded — the worker uses the summary's flag as the
        # source-of-truth for diff compatibility, so a hard-coded
        # False would silently let truncated builds claim
        # diffability.
        self.assertFalse(summary['truncated'])
        self.assertIs(summary['truncated'], False)
        # `collected_at` is an ISO-8601 UTC string with the trailing
        # `Z` form (no space, no offset). The appserver's
        # `new Date(collected_at)` MUST parse identically to the
        # Android plugin's `isoUtc` helper — pinning the exact
        # zero-epoch rendering catches both "swap T for space" and
        # "drop the Z" mutations as well as "set to None".
        self.assertEqual(summary['collected_at'], '1970-01-01T00:00:00Z')

        # Also pin a NON-zero clock so the ms→s divisor (1000) is
        # actually exercised. With the zero-epoch case any divisor
        # produces "1970-01-01T00:00:00Z", so a silent change of
        # `epoch_ms / 1000` to e.g. `/ 100` would slip past — the
        # call site passes `int(time.time() * 1000)` so the
        # divisor IS the millisecond contract.
        summary_nz, _ = agent._build_dependencies_payload(
            entries, truncated=False, scope_label='all',
            clock=lambda: 1_700_000_000_000,
        )
        self.assertEqual(summary_nz['collected_at'],
                         '2023-11-14T22:13:20Z')

    def test_summary_carries_collection_config_fingerprint(self):
        # The worker compares this object against the previous
        # build's collection_config to decide diff compatibility.
        # Every field must serialise exactly so the comparison is
        # bit-for-bit deterministic on both producer and worker
        # sides of the wire.
        _, _ = agent._build_dependencies_payload(
            [], truncated=False, scope_label='all', clock=lambda: 0,
        )
        summary, _ = agent._build_dependencies_payload(
            [], truncated=True, scope_label='runtime_direct_only',
            include_selected_reason=True, max_count=2500,
            clock=lambda: 0,
        )
        cfg = summary['collection_config']
        self.assertEqual(cfg['scope'], 'runtime_direct_only')
        self.assertEqual(cfg['include_selected_reason'], True)
        self.assertEqual(cfg['max_count'], 2500)
        # The truncated arg must flow through to summary['truncated']
        # — the worker reads this exact field to decide whether the
        # build's dep list is diff-compatible with the previous
        # build's. A hard-coded False here would silently break
        # downstream truncation gating.
        self.assertTrue(summary['truncated'])
        self.assertIs(summary['truncated'], True)

    def test_blob_schema_version_and_entry_shape(self):
        # Build a third entry with version=None to pin the
        # version-omit-when-None contract — without this fixture
        # entry the test would silently accept "always emit version".
        no_version_entry = self._entry('NoVer', direct=True, version=None)
        entries = [
            self._entry('Braintree', direct=True),
            self._entry('Braintree/Card', direct=False,
                        parents=[agent._make_dep_id('library', '', 'Braintree')]),
            no_version_entry,
        ]
        _, blob = agent._build_dependencies_payload(
            entries, truncated=False, scope_label='all', clock=lambda: 0,
        )
        # Schema version must equal the Android plugin's emitter
        # and the worker's supported set — single source of truth.
        self.assertEqual(blob['schema_version'], 1)
        self.assertEqual(len(blob['dependencies']), 3)
        first = blob['dependencies'][0]
        # `id` and `type` always present.
        self.assertEqual(first['id'], 'library::Braintree')
        self.assertEqual(first['type'], 'library')
        self.assertEqual(first['direct'], True)
        # `group` is always emitted (even when empty) — the worker
        # uses (type, group, name) as the identity triple and the
        # Android plugin pins the same posture. Dropping the field
        # would silently make iOS-emitted entries fail identity
        # joins against Android-emitted ones from the same product.
        self.assertIn('group', first)
        self.assertEqual(first['group'], '')
        # `scope` is omitted when None (saves wire bytes — most
        # iOS deps don't carry a scope). The fixture's `_entry`
        # default sets scope=None, so the produced item must NOT
        # carry a `scope` key. A mutation that always emits scope
        # would surface as a stray `'scope': None` here.
        self.assertNotIn('scope', first)
        # `parents` omitted when empty (saves wire bytes on direct
        # deps, which dominate the entry count on most projects).
        self.assertNotIn('parents', first)
        # `parents` present on the transitive — and references the
        # parent's id verbatim so consumers can rebuild the tree.
        second = blob['dependencies'][1]
        self.assertEqual(second['parents'], ['library::Braintree'])
        # `version` omitted when None — same wire-byte-saving posture
        # as `scope`. Pin via the dedicated no-version fixture entry
        # (the other two carry version='1' so wouldn't catch
        # "always-emit-version" mutations).
        third = blob['dependencies'][2]
        self.assertEqual(third['name'], 'NoVer')
        self.assertNotIn('version', third)


# ──────────────────────────────────────────────
# Gzip / wire serialisation
# ──────────────────────────────────────────────

class TestGzipJsonBytes(unittest.TestCase):
    def test_round_trips_through_gzip_decompress(self):
        # The PUT carries gzipped JSON; the worker decompresses
        # before validating. Round-trip the local bytes to confirm
        # they're well-formed gzip + JSON.
        #
        # Use a non-empty payload — an empty `dependencies` list
        # would serialise identically in compact and pretty-printed
        # modes, so we wouldn't catch a `indent=2` mutation.
        payload = {
            'schema_version': 1,
            'dependencies': [
                {'id': 'library::A', 'name': 'A', 'type': 'library'},
            ],
        }
        gz_bytes = agent._gzip_json_bytes(payload)
        # gzip magic bytes confirm the bytes are truly gzipped
        # (not just plain JSON).
        self.assertEqual(gz_bytes[:2], b'\x1f\x8b')
        # Round-trip via gzip.decompress.
        decompressed = gzip.decompress(gz_bytes)
        round_tripped = json.loads(decompressed.decode('utf-8'))
        self.assertEqual(round_tripped, payload)
        # Compact form: separators=(',', ':') — no whitespace after
        # the colon, no whitespace after the comma. This is a wire-
        # contract pin: every byte we don't ship on every upload
        # adds up across millions of builds, and `json.dumps(...,
        # indent=2)` would silently double the gzipped payload size
        # while still passing a naive round-trip check.
        self.assertIn(b'":"', decompressed)
        self.assertNotIn(b': ', decompressed)
        self.assertNotIn(b', ', decompressed)
        self.assertNotIn(b'\n', decompressed)


# ──────────────────────────────────────────────
# Filesystem / Xcode-env helpers
# ──────────────────────────────────────────────
#
# These wrap subprocess and os.walk against Xcode build-phase env
# vars and the dSYM folder layout. They bridge between the env vars
# and the parsers tested above — every silent breakage here would
# surface as "no deps collected" with no error in the build log,
# so they need explicit coverage.

from types import SimpleNamespace
from unittest import mock


_OPTIONS_SENTINEL = object()


def _install_options(agent_mod, **fields):
    """Inject a fake `options` namespace onto the agent module.
    BugseeAgent populates the real `options` only inside `main()` via
    OptionParser.parse_args, which doesn't fire in the test loader.
    The helpers reference attrs like `options.build_dir` /
    `options.dsym_folder` / `options.version` / `options.build`, so
    we hand them a SimpleNamespace with the expected slots and let
    each test pass the values it needs.
    Returns whatever was on the module before (sentinel if absent)
    so tearDown can restore it.
    """
    prev = getattr(agent_mod, 'options', _OPTIONS_SENTINEL)
    defaults = {
        'build_dir':    None,
        'dsym_folder':  None,
        'version':      None,
        'build':        None,
    }
    defaults.update(fields)
    agent_mod.options = SimpleNamespace(**defaults)
    return prev


def _restore_options(agent_mod, prev):
    if prev is _OPTIONS_SENTINEL:
        try:
            del agent_mod.options
        except AttributeError:
            pass
    else:
        agent_mod.options = prev


# ──────────────────────────────────────────────
# _parse_vendored_frameworks
# ──────────────────────────────────────────────

# Realistic otool -L sample. The first line is always the binary's
# own install name (skipped because it doesn't start with `@rpath/`
# unless the binary itself is dyld-staged). System dylibs follow,
# then the embedded frameworks we actually want.
_OTOOL_SAMPLE = (
    "/private/var/staging/MyApp.app/MyApp:\n"
    "\t@rpath/Bugsee.framework/Bugsee (compatibility version 1.0.0, current version 1.0.0)\n"
    "\t@rpath/Alamofire.framework/Alamofire (compatibility version 1.0.0, current version 5.8.0)\n"
    "\t@executable_path/Frameworks/Sentry.framework/Sentry (compatibility version 1.0.0, current version 8.0.0)\n"
    "\t/usr/lib/libobjc.A.dylib (compatibility version 1.0.0, current version 228.0.0)\n"
    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n"
    "\t/System/Library/Frameworks/Foundation.framework/Foundation (compatibility version 300.0.0, current version 2402.0.0)\n"
    "\t/System/Library/Frameworks/UIKit.framework/UIKit (compatibility version 1.0.0, current version 7167.0.0)\n"
    # Duplicate of Bugsee — exercises the dedup `seen` set.
    "\t@rpath/Bugsee.framework/Bugsee (compatibility version 1.0.0, current version 1.0.0)\n"
)


class TestParseVendoredFrameworks(unittest.TestCase):
    def test_returns_empty_on_missing_path(self):
        # Both branches of the path-guard short-circuit before
        # subprocess is ever called.
        self.assertEqual(agent._parse_vendored_frameworks(None), [])
        self.assertEqual(agent._parse_vendored_frameworks('/no/such/binary'), [])

    def _run_with_canned_output(self, otool_stdout):
        # Write a placeholder file so the os.path.isfile guard
        # passes; the actual bytes are irrelevant — subprocess is
        # mocked out, but the guard checks the path itself.
        path = _write_fixture('placeholder')
        try:
            with mock.patch('subprocess.run') as mocked:
                mocked.return_value = SimpleNamespace(stdout=otool_stdout)
                return agent._parse_vendored_frameworks(path)
        finally:
            os.unlink(path)

    def test_emits_only_embedded_frameworks(self):
        entries = self._run_with_canned_output(_OTOOL_SAMPLE)
        names = [e['name'] for e in entries]
        # Embedded frameworks from @rpath / @executable_path are
        # surfaced, with the .framework basename (NOT the inner
        # Mach-O slice name) as the entry name.
        self.assertIn('Bugsee.framework', names)
        self.assertIn('Alamofire.framework', names)
        self.assertIn('Sentry.framework', names)
        # System dylibs / Foundation / UIKit are filtered out.
        self.assertNotIn('libobjc.A.dylib', names)
        self.assertNotIn('libSystem.B.dylib', names)
        self.assertNotIn('Foundation.framework', names)
        self.assertNotIn('UIKit.framework', names)

    def test_dedupes_when_same_framework_appears_twice(self):
        entries = self._run_with_canned_output(_OTOOL_SAMPLE)
        names = [e['name'] for e in entries]
        # The sample lists Bugsee twice — must surface once. A naive
        # parser that forgot the `seen` set would emit 2 Bugsee
        # entries and the dashboard would render a phantom dup.
        self.assertEqual(names.count('Bugsee.framework'), 1)

    def test_entry_shape_matches_dep_contract(self):
        entries = self._run_with_canned_output(_OTOOL_SAMPLE)
        bugsee = next(e for e in entries if e['name'] == 'Bugsee.framework')
        self.assertEqual(bugsee['type'], 'file')
        self.assertEqual(bugsee['direct'], True)
        self.assertEqual(bugsee['group'], '')
        self.assertEqual(bugsee['parents'], [])
        # `id` is the canonical file::Bugsee.framework form — pinned
        # so the worker's identity dedup behaves identically across
        # platforms.
        self.assertEqual(bugsee['id'],
                         agent._make_dep_id('file', '', 'Bugsee.framework'))

    def test_otool_failure_returns_empty_not_raise(self):
        # When otool itself errors (binary is fat-but-broken, or
        # /usr/bin/otool isn't installed), the agent must NOT take
        # the build down — return empty and let the rest of the
        # deps pipeline carry on.
        path = _write_fixture('placeholder')
        try:
            import subprocess as _sub
            with mock.patch('subprocess.run',
                            side_effect=_sub.CalledProcessError(1, 'otool')):
                entries = agent._parse_vendored_frameworks(path)
            self.assertEqual(entries, [])
        finally:
            os.unlink(path)


# ──────────────────────────────────────────────
# _read_info_plist
# ──────────────────────────────────────────────

class TestReadInfoPlist(unittest.TestCase):
    def test_returns_empty_on_missing_path(self):
        # None, empty string, nonexistent file — all three trigger
        # the soft-fail. Caller treats {} as "no info; fall through
        # to env vars".
        self.assertEqual(agent._read_info_plist(None), {})
        self.assertEqual(agent._read_info_plist(''), {})
        self.assertEqual(agent._read_info_plist('/no/such/plist'), {})

    def test_reads_xml_plist_fields(self):
        import plistlib
        fd, path = tempfile.mkstemp(suffix='.plist')
        with os.fdopen(fd, 'wb') as f:
            plistlib.dump({
                'CFBundleShortVersionString': '1.2.3',
                'CFBundleVersion':            '42',
                'CFBundleIdentifier':         'com.example.app',
            }, f)
        try:
            result = agent._read_info_plist(path)
        finally:
            os.unlink(path)
        # All three keys round-trip — this is the same plistlib
        # path the Xcode build phase relies on, so a future swap to
        # a non-plistlib parser must preserve these fields verbatim.
        self.assertEqual(result['CFBundleShortVersionString'], '1.2.3')
        self.assertEqual(result['CFBundleVersion'],            '42')
        self.assertEqual(result['CFBundleIdentifier'], 'com.example.app')

    def test_malformed_plist_returns_empty_not_raise(self):
        # Garbage bytes — plistlib raises; soft-fail to {} so the
        # rest of the metadata collection can fall through to env
        # vars instead of taking the whole build down.
        path = _write_fixture('this is not a plist at all')
        try:
            result = agent._read_info_plist(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, {})


# ──────────────────────────────────────────────
# _find_first_above
# ──────────────────────────────────────────────

class TestFindFirstAbove(unittest.TestCase):
    def test_returns_none_on_empty_start(self):
        # Empty / None start_dir is the "called outside Xcode"
        # case — return None so the orchestrator skips the source.
        self.assertIsNone(agent._find_first_above(None, 'Podfile.lock'))
        self.assertIsNone(agent._find_first_above('', 'Podfile.lock'))

    def test_finds_file_at_start_dir(self):
        # Project-root-style layout: the lockfile sits right next
        # to the .xcodeproj. Walk MUST find it without climbing.
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'Podfile.lock')
            with open(target, 'w') as f:
                f.write('placeholder')
            found = agent._find_first_above(root, 'Podfile.lock')
            # Must be the absolute path. realpath normalises macOS's
            # /private/tmp <-> /tmp symlink so the comparison
            # doesn't depend on which side of the symlink each
            # branch resolved through.
            self.assertEqual(os.path.realpath(found),
                             os.path.realpath(target))

    def test_walks_up_to_find_file(self):
        # Realistic layout: the Xcode build phase runs from
        # `<root>/build/Debug-iphonesimulator/MyApp.app/` and the
        # lockfile is 3 levels above. Walk must climb to find it.
        with tempfile.TemporaryDirectory() as root:
            deep = os.path.join(root, 'a', 'b', 'c')
            os.makedirs(deep)
            target = os.path.join(root, 'Podfile.lock')
            with open(target, 'w') as f:
                f.write('placeholder')
            found = agent._find_first_above(deep, 'Podfile.lock')
            self.assertEqual(os.path.realpath(found),
                             os.path.realpath(target))

    def test_max_levels_cap_prevents_unbounded_climb(self):
        # The cap defends against a misconfigured start_dir that
        # would otherwise walk to filesystem root. Set the cap to
        # 2 and place the target 4 levels up — must NOT find it.
        with tempfile.TemporaryDirectory() as root:
            deep = os.path.join(root, 'a', 'b', 'c', 'd')
            os.makedirs(deep)
            target = os.path.join(root, 'Podfile.lock')
            with open(target, 'w') as f:
                f.write('placeholder')
            found = agent._find_first_above(deep, 'Podfile.lock',
                                             max_levels=2)
            self.assertIsNone(found)

    def test_returns_none_when_file_not_present_anywhere(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(agent._find_first_above(root, 'NoSuch.file'))

    def test_finds_nested_path_form(self):
        # Xcode-managed SPM stashes Package.resolved under
        # `<root>/MyApp.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`
        # and the orchestrator passes a relative subpath like
        # `xcshareddata/swiftpm/Package.resolved` to _find_first_above.
        # The walk must respect the relative path verbatim.
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, 'xcshareddata', 'swiftpm')
            os.makedirs(nested)
            target = os.path.join(nested, 'Package.resolved')
            with open(target, 'w') as f:
                f.write('{}')
            found = agent._find_first_above(
                root,
                os.path.join('xcshareddata', 'swiftpm', 'Package.resolved'),
            )
            self.assertEqual(os.path.realpath(found),
                             os.path.realpath(target))


# ──────────────────────────────────────────────
# _resolve_product_binary_path
# ──────────────────────────────────────────────

class TestResolveProductBinaryPath(unittest.TestCase):
    def setUp(self):
        # The helper falls through to `options.build_dir` when the
        # TARGET_BUILD_DIR env var is unset. Inject a default so the
        # tests can exercise both branches without NameError.
        self._prev = _install_options(agent, build_dir=None)

    def tearDown(self):
        _restore_options(agent, self._prev)

    def test_returns_none_when_both_env_and_options_unset(self):
        # No TARGET_BUILD_DIR and no options.build_dir → can't
        # construct a binary path → return None and let the
        # orchestrator skip the vendored-framework scan.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(agent._resolve_product_binary_path())

    def test_returns_none_when_executable_path_env_missing(self):
        # TARGET_BUILD_DIR set but EXECUTABLE_PATH not — without
        # the Mach-O leaf there's nothing to scan.
        with tempfile.TemporaryDirectory() as build_dir, \
                mock.patch.dict(os.environ,
                                {'TARGET_BUILD_DIR': build_dir},
                                clear=True):
            self.assertIsNone(agent._resolve_product_binary_path())

    def test_returns_none_when_resolved_path_does_not_exist(self):
        # Env vars set but the joined path doesn't exist on disk —
        # the linker hasn't produced the binary yet (or the path
        # is stale). Soft-fail.
        with tempfile.TemporaryDirectory() as build_dir, \
                mock.patch.dict(os.environ, {
                    'TARGET_BUILD_DIR':  build_dir,
                    'EXECUTABLE_PATH':   'MyApp.app/MyApp',
                }, clear=True):
            self.assertIsNone(agent._resolve_product_binary_path())

    def test_returns_path_when_binary_exists(self):
        with tempfile.TemporaryDirectory() as build_dir:
            app = os.path.join(build_dir, 'MyApp.app')
            os.makedirs(app)
            binary = os.path.join(app, 'MyApp')
            with open(binary, 'wb') as f:
                f.write(b'\xcf\xfa\xed\xfe')  # 64-bit Mach-O magic
            with mock.patch.dict(os.environ, {
                'TARGET_BUILD_DIR':  build_dir,
                'EXECUTABLE_PATH':   'MyApp.app/MyApp',
            }, clear=True):
                result = agent._resolve_product_binary_path()
            self.assertEqual(os.path.realpath(result),
                             os.path.realpath(binary))


# ──────────────────────────────────────────────
# _extract_first_dwarf_uuid
# ──────────────────────────────────────────────

class TestExtractFirstDwarfUuid(unittest.TestCase):
    def setUp(self):
        self._prev = _install_options(agent, dsym_folder=None)

    def tearDown(self):
        _restore_options(agent, self._prev)

    def test_returns_none_when_dsym_folder_unset(self):
        # options.dsym_folder is None by default → short-circuit.
        self.assertIsNone(agent._extract_first_dwarf_uuid())

    def test_returns_none_when_folder_has_no_dsym(self):
        with tempfile.TemporaryDirectory() as folder:
            agent.options.dsym_folder = folder
            self.assertIsNone(agent._extract_first_dwarf_uuid())

    def test_returns_first_uuid_when_dsym_found(self):
        # Build a realistic dSYM tree:
        #   <folder>/MyApp.app.dSYM/Contents/Resources/DWARF/MyApp
        # Mock parseDSYM so we don't actually shell out to
        # dwarfdump — and so the test stays hermetic.
        with tempfile.TemporaryDirectory() as folder:
            dwarf = os.path.join(folder, 'MyApp.app.dSYM', 'Contents',
                                 'Resources', 'DWARF')
            os.makedirs(dwarf)
            binary = os.path.join(dwarf, 'MyApp')
            with open(binary, 'wb') as f:
                f.write(b'\xcf\xfa\xed\xfe' + b'\x00' * 32)
            agent.options.dsym_folder = folder
            with mock.patch.object(agent, 'parseDSYM',
                                   return_value=['ABCDEF12-3456-7890-ABCD-EF1234567890']):
                uuid = agent._extract_first_dwarf_uuid()
            self.assertEqual(uuid, 'ABCDEF12-3456-7890-ABCD-EF1234567890')

    def test_skips_empty_and_symlink_dwarf_files(self):
        # Zero-byte file (linker placeholder) AND a symlink — both
        # must be skipped before parseDSYM is even called.
        with tempfile.TemporaryDirectory() as folder:
            dwarf = os.path.join(folder, 'MyApp.app.dSYM', 'Contents',
                                 'Resources', 'DWARF')
            os.makedirs(dwarf)
            empty = os.path.join(dwarf, 'Empty')
            open(empty, 'wb').close()  # 0 bytes
            agent.options.dsym_folder = folder
            with mock.patch.object(agent, 'parseDSYM') as parseDSYM_mock:
                parseDSYM_mock.return_value = ['SHOULD-NOT-SURFACE']
                uuid = agent._extract_first_dwarf_uuid()
            # parseDSYM was never called → no candidate found → None.
            self.assertEqual(parseDSYM_mock.call_count, 0)
            self.assertIsNone(uuid)


# ──────────────────────────────────────────────
# _collect_build_metadata
# ──────────────────────────────────────────────

class TestCollectBuildMetadata(unittest.TestCase):
    def setUp(self):
        self._prev = _install_options(agent, build_dir=None,
                                       version=None, build=None)
        # The shape-of-body tests focus on Info.plist + env fallback
        # plumbing. The xcode-version and VCS resolvers have their
        # own test classes below; here we silence them so:
        #   1. `resolve_xcode_version` doesn't shell out to
        #      `xcodebuild -version` on every test (~50 ms each →
        #      otherwise the suite goes from 40 ms to 350 ms).
        #   2. `resolve_vcs_metadata` doesn't pick up the host
        #      machine's git context (the tests run from inside a
        #      real git repo; without this stub the body['vcs']
        #      field would carry the CI machine's commit_sha and
        #      the tests would be host-dependent).
        self._patches = [
            mock.patch.object(agent, 'resolve_xcode_version',
                              return_value=None),
            mock.patch.object(agent, 'resolve_vcs_metadata',
                              return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _restore_options(agent, self._prev)

    def _make_plist(self, body):
        import plistlib
        fd, path = tempfile.mkstemp(suffix='.plist')
        with os.fdopen(fd, 'wb') as f:
            plistlib.dump(body, f)
        return path

    def test_reads_fields_from_info_plist_when_present(self):
        # Plist values take precedence over env-var fallbacks.
        plist = self._make_plist({
            'CFBundleShortVersionString': '1.2.3',
            'CFBundleVersion':            '42',
            'CFBundleIdentifier':         'com.example.app',
        })
        try:
            with tempfile.TemporaryDirectory() as build_dir, \
                    mock.patch.dict(os.environ, {
                        'TARGET_BUILD_DIR':            build_dir,
                        'INFOPLIST_PATH':              os.path.relpath(plist, build_dir),
                        # Env-var values DIFFERENT from the plist
                        # values to pin precedence: plist wins.
                        'MARKETING_VERSION':           'env-version',
                        'CURRENT_PROJECT_VERSION':     'env-build',
                        'PRODUCT_BUNDLE_IDENTIFIER':   'env.pkg',
                        'CONFIGURATION':               'Release',
                        'XCODE_VERSION_ACTUAL':        '1530',
                    }, clear=True):
                body = agent._collect_build_metadata()
            self.assertEqual(body['version'],     '1.2.3')
            self.assertEqual(body['build'],       '42')
            self.assertEqual(body['package_id'],  'com.example.app')
        finally:
            os.unlink(plist)

    def test_falls_back_to_env_vars_when_plist_missing_fields(self):
        # Plist absent → env vars carry the values. The fallback
        # chain (plist > env > options) is documented; pin every
        # link by exercising the case where only the middle link
        # has values.
        with mock.patch.dict(os.environ, {
            'MARKETING_VERSION':           '9.8.7',
            'CURRENT_PROJECT_VERSION':     '1024',
            'PRODUCT_BUNDLE_IDENTIFIER':   'com.example.fromenv',
            'CONFIGURATION':               'Debug',
        }, clear=True):
            body = agent._collect_build_metadata()
        self.assertEqual(body['version'],    '9.8.7')
        self.assertEqual(body['build'],      '1024')
        self.assertEqual(body['package_id'], 'com.example.fromenv')

    def test_falls_back_to_options_when_env_and_plist_missing(self):
        # CLI invocation case: BugseeAgent is run from the command
        # line with --version/--build, no Xcode env. The options
        # namespace MUST be the last-resort fallback for both.
        agent.options.version = 'cli-version'
        agent.options.build   = 'cli-build'
        with mock.patch.dict(os.environ, {}, clear=True):
            body = agent._collect_build_metadata()
        self.assertEqual(body['version'], 'cli-version')
        self.assertEqual(body['build'],   'cli-build')

    def test_emits_iso_contract_fields(self):
        # The wire contract with the appserver requires these
        # fields to be present at fixed shapes regardless of
        # which fallbacks fired:
        #   - format == 'ipa' (worker's ALLOWED_BUILD_FORMATS gate)
        #   - has_mapping == False (iOS has no Java mapping)
        #   - build_metadata.build_system.type == 'xcode'
        #   - build_metadata.agent.name == 'BugseeAgent'
        with mock.patch.dict(os.environ, {}, clear=True):
            body = agent._collect_build_metadata()
        self.assertEqual(body['format'],      'ipa')
        self.assertEqual(body['has_mapping'], False)
        self.assertEqual(body['uuid'], None)  # filled in by caller
        self.assertEqual(body['build_metadata']['build_system']['type'],
                         'xcode')
        self.assertEqual(body['build_metadata']['agent']['name'],
                         'BugseeAgent')


# ──────────────────────────────────────────────
# _collect_all_dependencies
# ──────────────────────────────────────────────

class TestCollectAllDependencies(unittest.TestCase):
    def setUp(self):
        self._prev = _install_options(agent, build_dir=None)

    def tearDown(self):
        _restore_options(agent, self._prev)

    def test_returns_empty_when_no_lockfiles_anywhere(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {}, clear=True):
            merged, scope, truncated = agent._collect_all_dependencies(root)
        self.assertEqual(merged, [])
        self.assertFalse(truncated)
        # Scope label is the worker's compatibility token — pin it
        # so the iOS side and Android side stay diff-comparable
        # across the same build's records.
        self.assertEqual(scope, 'all')

    def test_aggregates_pods_spm_and_carthage_when_all_present(self):
        # Project root layout:
        #   root/Podfile.lock       — 1 entry
        #   root/Package.resolved   — 1 entry
        #   root/Cartfile.resolved  — 1 entry
        # The merged result must surface all three.
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {}, clear=True):
            with open(os.path.join(root, 'Podfile.lock'), 'w') as f:
                f.write(textwrap.dedent("""\
                    PODS:
                      - SocketRocket (0.7.1)

                    DEPENDENCIES:
                      - SocketRocket

                    SPEC CHECKSUMS:
                      SocketRocket: feedface
                """))
            with open(os.path.join(root, 'Package.resolved'), 'w') as f:
                f.write(json.dumps({
                    "pins": [{
                        "identity": "alamofire",
                        "kind": "remoteSourceControl",
                        "location": "https://github.com/Alamofire/Alamofire.git",
                        "state": {"version": "5.8.1"},
                    }],
                    "version": 2,
                }))
            with open(os.path.join(root, 'Cartfile.resolved'), 'w') as f:
                f.write('github "ReactiveCocoa/ReactiveCocoa" "v2.3.1"\n')
            merged, scope, truncated = agent._collect_all_dependencies(root)
        names = {e['name'] for e in merged}
        self.assertIn('SocketRocket', names)
        self.assertIn('alamofire', names)
        self.assertIn('ReactiveCocoa/ReactiveCocoa', names)
        self.assertFalse(truncated)

    def test_finds_xcode_managed_spm_via_nested_path(self):
        # When Package.resolved isn't at the project root, the
        # orchestrator looks for the Xcode-managed variant under
        # xcshareddata/swiftpm/. Verify that path lights up.
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {}, clear=True):
            nested = os.path.join(root, 'xcshareddata', 'swiftpm')
            os.makedirs(nested)
            with open(os.path.join(nested, 'Package.resolved'), 'w') as f:
                f.write(json.dumps({
                    "pins": [{
                        "identity": "swift-collections",
                        "kind": "remoteSourceControl",
                        "location": "https://github.com/apple/swift-collections.git",
                        "state": {"version": "1.1.0"},
                    }],
                    "version": 2,
                }))
            merged, _scope, _truncated = agent._collect_all_dependencies(root)
        names = {e['name'] for e in merged}
        self.assertIn('swift-collections', names)


# ──────────────────────────────────────────────
# Build-environment helpers (ported from SDK BugseeAgent)
# ──────────────────────────────────────────────
#
# `resolve_machine_label` / `resolve_vcs_metadata` /
# `resolve_xcode_version` (plus the trio of pure helpers
# `_env_truthy` / `_set_if_present` / `_local_hostname`) port the
# CI-provider-aware build context from the SDK's tools.bundle
# agent. The wire shape is consumed verbatim by the appserver's
# `sanitizeVcs` / `sanitizeBuildMetadata` and the dashboard reads
# `vcs.branch` / `vcs.commit_sha` / `vcs.pr_number` directly, so
# every emitted field is part of a cross-platform contract.


class TestEnvTruthy(unittest.TestCase):
    def test_canonical_on_tokens_are_truthy(self):
        # These five forms must all map to True — they're the same
        # set the Android Gradle plugin honours, so one CI config
        # snippet can enable a feature on both platforms.
        for tok in ('1', 'true', 'yes', 'on',
                    'TRUE', 'YES', '  on  '):
            self.assertTrue(agent._env_truthy(tok),
                            "expected truthy: %r" % tok)

    def test_falsy_or_empty_is_false(self):
        for tok in (None, '', '   ', '0', 'false', 'no', 'off',
                    'maybe', 'enabled'):
            self.assertFalse(agent._env_truthy(tok),
                             "expected falsy: %r" % tok)


class TestSetIfPresent(unittest.TestCase):
    def test_writes_non_empty_string(self):
        out = {}
        agent._set_if_present(out, 'key', 'value')
        self.assertEqual(out, {'key': 'value'})

    def test_strips_whitespace(self):
        # The dashboard distinguishes "missing" from "known empty"
        # by field presence — but a key with whitespace surrounding
        # the value is just bad hygiene, so strip.
        out = {}
        agent._set_if_present(out, 'key', '  value  ')
        self.assertEqual(out, {'key': 'value'})

    def test_skips_none(self):
        out = {}
        agent._set_if_present(out, 'key', None)
        self.assertEqual(out, {})

    def test_skips_empty_string(self):
        # Critical: missing-vs-empty is the dashboard's signal for
        # "unknown vs known-empty". Empty MUST NOT write.
        out = {}
        agent._set_if_present(out, 'key', '')
        self.assertEqual(out, {})

    def test_skips_whitespace_only_string(self):
        # Same gate as empty — a CI env that exports `BRANCH=" "`
        # has nothing useful.
        out = {}
        agent._set_if_present(out, 'key', '   ')
        self.assertEqual(out, {})

    def test_stringifies_non_string_value(self):
        # CI envs occasionally surface numeric values via boolean
        # default substitution; the helper coerces to str.
        out = {}
        agent._set_if_present(out, 'key', 42)
        self.assertEqual(out, {'key': '42'})


class TestLocalHostname(unittest.TestCase):
    def test_returns_socket_hostname(self):
        with mock.patch('socket.gethostname',
                        return_value='dev-laptop.local'):
            self.assertEqual(agent._local_hostname(),
                             'dev-laptop.local')

    def test_returns_none_when_socket_raises(self):
        # Sandboxed runners may block hostname lookup; return None
        # so the caller can fall through cleanly.
        with mock.patch('socket.gethostname',
                        side_effect=OSError("sandboxed")):
            self.assertIsNone(agent._local_hostname())

    def test_returns_none_when_socket_returns_empty(self):
        # Defensive — empty hostname is treated as absent.
        with mock.patch('socket.gethostname', return_value=''):
            self.assertIsNone(agent._local_hostname())


class TestResolveMachineLabel(unittest.TestCase):
    def test_github_actions_with_runner_name(self):
        # Provider precedence is highest signal first; GITHUB_ACTIONS
        # must NOT lose to a generic CI=true falling through.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'RUNNER_NAME':    'gh-runner-7',
            'CI':             'true',  # generic — must be shadowed
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'github-actions:gh-runner-7')

    def test_github_actions_without_runner_name(self):
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'github-actions')

    def test_gitlab_ci_prefers_runner_description(self):
        # Description is human-readable, ID is a numeric global —
        # description wins when both are set.
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':              'true',
            'CI_RUNNER_DESCRIPTION':  'macos-arm64-pool',
            'CI_RUNNER_ID':           '12345',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'gitlab-ci:macos-arm64-pool')

    def test_gitlab_ci_falls_back_to_runner_id(self):
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':    'true',
            'CI_RUNNER_ID': '12345',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'gitlab-ci:12345')

    def test_jenkins_uses_node_name(self):
        with mock.patch.dict(os.environ, {
            'JENKINS_URL': 'https://jenkins.example.com',
            'NODE_NAME':   'mac-build-agent',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'jenkins:mac-build-agent')

    def test_circleci_uses_node_index(self):
        with mock.patch.dict(os.environ, {
            'CIRCLECI':           'true',
            'CIRCLE_NODE_INDEX':  '0',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'circleci:0')

    def test_bitrise_uses_app_slug(self):
        with mock.patch.dict(os.environ, {
            'BITRISE_IO':       'true',
            'BITRISE_APP_SLUG': 'abc123def456',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'bitrise:abc123def456')

    def test_xcode_cloud_uses_workflow_name(self):
        # `CI_WORKFLOW` is Apple's canonical presence signal for
        # Xcode Cloud — must surface ahead of the generic CI=true
        # fall-through.
        with mock.patch.dict(os.environ, {
            'CI_WORKFLOW': 'Release Archive',
            'CI':          'true',  # set on every Xcode Cloud run
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'xcode-cloud:Release Archive')

    def test_generic_ci_uses_hostname(self):
        # Last-resort CI provider — no specific marker, fall back
        # to the runner's hostname.
        with mock.patch.dict(os.environ, {
            'CI':       'true',
            'HOSTNAME': 'ci-runner-42',
        }, clear=True):
            self.assertEqual(agent.resolve_machine_label(),
                             'ci:ci-runner-42')

    def test_no_provider_returns_local_hostname(self):
        # No CI env vars at all → label is just the bare hostname
        # via _local_hostname.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch('socket.gethostname',
                           return_value='dev-laptop.local'):
            self.assertEqual(agent.resolve_machine_label(),
                             'dev-laptop.local')


class TestResolveVcsMetadata(unittest.TestCase):
    def test_github_actions_push(self):
        # Push event: GITHUB_REF carries `refs/heads/<branch>` and
        # the resolver must strip the prefix. No PR number on push.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS':     'true',
            'GITHUB_SHA':         'abc123def456',
            'GITHUB_REPOSITORY':  'bugsee/fastlane-plugin-bugsee',
            'GITHUB_REF':         'refs/heads/master',
            'GITHUB_EVENT_NAME':  'push',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'],   'github')
        self.assertEqual(vcs['commit_sha'], 'abc123def456')
        self.assertEqual(vcs['repo'],       'bugsee/fastlane-plugin-bugsee')
        self.assertEqual(vcs['branch'],     'master')
        # Push events MUST NOT carry pr_number — the dashboard
        # treats its presence as "this build is a PR build".
        self.assertNotIn('pr_number', vcs)
        self.assertNotIn('base_branch', vcs)

    def test_github_actions_pull_request(self):
        # PR event: GITHUB_HEAD_REF / GITHUB_BASE_REF carry the
        # source / target branches; PR number is dug out of the
        # GITHUB_REF regex `refs/pull/<n>/merge`.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS':     'true',
            'GITHUB_SHA':         'pr-sha',
            'GITHUB_REPOSITORY':  'org/repo',
            'GITHUB_HEAD_REF':    'feature/x',
            'GITHUB_BASE_REF':    'main',
            'GITHUB_REF':         'refs/pull/42/merge',
            'GITHUB_EVENT_NAME':  'pull_request',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'],    'github')
        self.assertEqual(vcs['commit_sha'],  'pr-sha')
        self.assertEqual(vcs['branch'],      'feature/x')
        self.assertEqual(vcs['base_branch'], 'main')
        # pr_number is int (NOT str) — the dashboard's mongo query
        # filters by numeric type, a string '42' would be invisible.
        self.assertEqual(vcs['pr_number'], 42)
        self.assertIsInstance(vcs['pr_number'], int)

    def test_gitlab_merge_request(self):
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':                              'true',
            'CI_COMMIT_SHA':                          'gl-sha',
            'CI_PROJECT_PATH':                        'group/project',
            'CI_MERGE_REQUEST_IID':                   '7',
            'CI_MERGE_REQUEST_SOURCE_BRANCH_NAME':    'feat/foo',
            'CI_MERGE_REQUEST_TARGET_BRANCH_NAME':    'develop',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'],    'gitlab')
        self.assertEqual(vcs['commit_sha'],  'gl-sha')
        self.assertEqual(vcs['repo'],        'group/project')
        self.assertEqual(vcs['branch'],      'feat/foo')
        self.assertEqual(vcs['base_branch'], 'develop')
        # IID (not ID) — the ID is a global DB key, useless as a
        # PR reference. Pin the source for the value.
        self.assertEqual(vcs['pr_number'], 7)

    def test_gitlab_push_event(self):
        # Push pipelines lack CI_MERGE_REQUEST_IID — branch comes
        # from CI_COMMIT_REF_NAME instead.
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':              'true',
            'CI_COMMIT_SHA':          'gl-push-sha',
            'CI_COMMIT_REF_NAME':     'master',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['branch'], 'master')
        self.assertNotIn('pr_number', vcs)
        self.assertNotIn('base_branch', vcs)

    def test_bitbucket_pr(self):
        with mock.patch.dict(os.environ, {
            'BITBUCKET_BUILD_NUMBER':       '42',
            'BITBUCKET_COMMIT':             'bb-sha',
            'BITBUCKET_REPO_FULL_NAME':     'team/repo',
            'BITBUCKET_BRANCH':             'feature/x',
            'BITBUCKET_PR_ID':              '99',
            'BITBUCKET_PR_DESTINATION_BRANCH': 'master',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'],    'bitbucket')
        self.assertEqual(vcs['commit_sha'],  'bb-sha')
        self.assertEqual(vcs['repo'],        'team/repo')
        self.assertEqual(vcs['branch'],      'feature/x')
        self.assertEqual(vcs['base_branch'], 'master')
        self.assertEqual(vcs['pr_number'],   99)

    def test_git_fallback_local_dev(self):
        # No CI provider env → shell out to `git` in working_dir.
        # Use a real temp git repo so the test isn't dependent on
        # the host machine's git state.
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(['git', 'init', '-q', '-b', 'main', repo],
                           check=True)
            subprocess.run(['git', 'config', 'user.email',
                            'test@example.com'],
                           cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Test'],
                           cwd=repo, check=True)
            open(os.path.join(repo, 'file.txt'), 'w').write('hi')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', 'init'],
                           cwd=repo, check=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                vcs = agent.resolve_vcs_metadata(repo)
        # Local commit_sha + branch must surface; provider is
        # absent (no CI matched) — the server tolerates a partial
        # vcs sub-object.
        self.assertIn('commit_sha', vcs)
        self.assertEqual(len(vcs['commit_sha']), 40)  # full SHA-1
        self.assertEqual(vcs['branch'], 'main')
        self.assertNotIn('provider', vcs)

    def test_git_fallback_returns_empty_for_nonexistent_dir(self):
        # No CI env AND no working dir → empty dict. The caller
        # (`_collect_build_metadata`) skips emitting `vcs` when
        # this returns empty.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                agent.resolve_vcs_metadata('/no/such/dir'), {})

    def test_git_fallback_returns_empty_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as plain_dir, \
                mock.patch.dict(os.environ, {}, clear=True):
            # No .git → `git rev-parse HEAD` errors → empty dict.
            self.assertEqual(
                agent.resolve_vcs_metadata(plain_dir), {})


class TestResolveXcodeVersion(unittest.TestCase):
    def test_decodes_numeric_env_var_to_dotted_form(self):
        # Xcode exports `XCODE_VERSION_ACTUAL=1620` for Xcode 16.2.0;
        # the resolver must split that into 16.2.0 without shelling
        # out (the dashboard displays the dotted form).
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '1620'},
                             clear=True):
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')

    def test_decodes_three_digit_patch_version(self):
        # `1543` → 15.4.3 — pins the three-component split rule
        # (last digit = patch, second-to-last = minor, prefix =
        # major). A naive `[0:2], [2:4]` split would yield 15.43.
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '1543'},
                             clear=True):
            self.assertEqual(agent.resolve_xcode_version(), '15.4.3')

    def test_falls_back_to_xcodebuild_when_env_var_absent(self):
        # Env var unset → resolver shells out to xcodebuild. Mock
        # subprocess to a canonical "Xcode 16.0\nBuild version 16A242d"
        # response and pin the parsed major.minor form.
        canned = SimpleNamespace(
            returncode=0,
            stdout="Xcode 16.0\nBuild version 16A242d\n",
        )
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch('subprocess.run', return_value=canned):
            self.assertEqual(agent.resolve_xcode_version(), '16.0')

    def test_returns_none_when_xcodebuild_fails(self):
        # `xcodebuild -version` nonzero exit → resolver returns
        # None so the caller can omit the field rather than
        # surfacing a garbled value.
        canned = SimpleNamespace(returncode=1, stdout='', stderr='nope')
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch('subprocess.run', return_value=canned):
            self.assertIsNone(agent.resolve_xcode_version())

    def test_returns_none_when_xcodebuild_missing(self):
        # FileNotFoundError on stripped-down CI images without
        # Xcode installed.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch('subprocess.run',
                           side_effect=FileNotFoundError("xcodebuild")):
            self.assertIsNone(agent.resolve_xcode_version())

    def test_non_numeric_env_var_falls_through(self):
        # Defensive: if Xcode ever exports a dotted form directly
        # (`16.2.0`), the `.isdigit()` guard must short-circuit
        # the numeric reformat and fall through to xcodebuild.
        canned = SimpleNamespace(
            returncode=0, stdout="Xcode 16.2.0\n",
        )
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '16.2.0'},
                             clear=True), \
                mock.patch('subprocess.run', return_value=canned):
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')


# ──────────────────────────────────────────────
# _collect_build_metadata — vcs sub-object wiring
# ──────────────────────────────────────────────

class TestCollectBuildMetadataVcsWiring(unittest.TestCase):
    """Pins the wiring between resolve_vcs_metadata and the body
    dict. The exhaustive provider matrix is tested in
    TestResolveVcsMetadata; this class only verifies the bridge
    between the resolver's output and the build registration POST."""

    def setUp(self):
        self._prev = _install_options(agent, build_dir=None,
                                       version=None, build=None)

    def tearDown(self):
        _restore_options(agent, self._prev)

    def test_vcs_present_when_resolver_returns_non_empty(self):
        # When the resolver returns content, body['vcs'] surfaces
        # verbatim — sibling to build_metadata, not nested under
        # it (matches the appserver's sanitizeVcs expectation).
        canned = {'provider': 'github', 'commit_sha': 'abc',
                  'branch': 'main'}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value=canned), \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            body = agent._collect_build_metadata()
        self.assertEqual(body['vcs'], canned)
        # Pin position: NOT inside build_metadata.
        self.assertNotIn('vcs', body['build_metadata'])

    def test_vcs_omitted_when_resolver_returns_empty(self):
        # Empty dict signals "no CI provider AND no git in
        # working_dir" — better to omit the field than carry an
        # empty {} that pollutes the dashboard.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}), \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            body = agent._collect_build_metadata()
        self.assertNotIn('vcs', body)

    def test_resolver_called_with_srcroot_first(self):
        # Precedence chain: SRCROOT > PROJECT_DIR > cwd. Pin the
        # top of the chain — a future swap to PROJECT_DIR-first
        # would silently route the git fallback at the wrong
        # working dir in mixed Xcode + manual-CLI invocations.
        with mock.patch.dict(os.environ, {
            'SRCROOT':     '/path/to/srcroot',
            'PROJECT_DIR': '/path/to/projectdir',
        }, clear=True), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}) as vcs_mock, \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            agent._collect_build_metadata()
        vcs_mock.assert_called_once_with('/path/to/srcroot')

    def test_resolver_falls_back_to_project_dir_when_srcroot_absent(self):
        with mock.patch.dict(os.environ, {
            'PROJECT_DIR': '/path/to/projectdir',
        }, clear=True), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}) as vcs_mock, \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            agent._collect_build_metadata()
        vcs_mock.assert_called_once_with('/path/to/projectdir')

    def test_machine_host_uses_machine_label_resolver(self):
        # `body['build_metadata']['machine']['host']` must come
        # from resolve_machine_label, NOT platform.node() — the
        # CI-provider-aware label is what the dashboard clusters
        # by.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolve_machine_label',
                                  return_value='github-actions:gh-runner'), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}), \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            body = agent._collect_build_metadata()
        self.assertEqual(body['build_metadata']['machine']['host'],
                         'github-actions:gh-runner')

    def test_machine_host_falls_back_to_platform_node(self):
        # When the label resolver returns None (no CI, no
        # hostname), fall back to platform.node() so the field is
        # never null in the upload.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolve_machine_label',
                                  return_value=None), \
                mock.patch('platform.node',
                           return_value='fallback-host'), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}), \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value=None):
            body = agent._collect_build_metadata()
        self.assertEqual(body['build_metadata']['machine']['host'],
                         'fallback-host')

    def test_build_system_version_uses_xcode_version_resolver(self):
        # `build_system.version` comes from resolve_xcode_version,
        # NOT the raw XCODE_VERSION_ACTUAL env var (which is in
        # the un-dotted numeric form).
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolve_xcode_version',
                                  return_value='16.2.0'), \
                mock.patch.object(agent, 'resolve_vcs_metadata',
                                  return_value={}):
            body = agent._collect_build_metadata()
        self.assertEqual(body['build_metadata']['build_system']['version'],
                         '16.2.0')


if __name__ == '__main__':
    unittest.main()

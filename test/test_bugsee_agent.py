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


if __name__ == '__main__':
    unittest.main()

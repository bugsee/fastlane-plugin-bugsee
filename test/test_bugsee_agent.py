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
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile


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

    def test_extracts_url_from_v2_location_field(self):
        # `location` is the v2/v3 SPM key; load-bearing for OSV
        # SwiftURL vuln lookups. Pre-fix, the Python in-process
        # fallback dropped this field — silently diverging from
        # both the Rust CLI and the SDK-side Python parser.
        body = json.dumps({
            "pins": [{
                "identity": "alamofire",
                "kind": "remoteSourceControl",
                "location": "https://github.com/Alamofire/Alamofire.git",
                "state": {"version": "5.8.1"},
            }],
            "version": 2,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)
        self.assertEqual(entries[0].get('url'),
                         'https://github.com/Alamofire/Alamofire.git')

    def test_extracts_url_from_v1_repositoryURL_field(self):
        # Legacy v1 SPM key. Must also map to the same `url`
        # output field so callers don't have to know about the
        # version split.
        body = json.dumps({
            "object": {
                "pins": [{
                    "package": "Alamofire",
                    "repositoryURL": "https://github.com/Alamofire/Alamofire.git",
                    "state": {"version": "5.8.1"},
                }],
            },
            "version": 1,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)
        self.assertEqual(entries[0].get('url'),
                         'https://github.com/Alamofire/Alamofire.git')

    def test_omits_url_key_when_neither_location_nor_repositoryURL_present(self):
        # Defensive: the field is optional in the wire shape (Rust
        # CLI uses `skip_serializing_if=Option::is_none`). The
        # Python side should likewise emit no `url` key rather
        # than writing `None`, so downstream key-membership checks
        # match the Rust output byte-for-byte.
        body = json.dumps({
            "pins": [{
                "identity": "weird-pin",
                "state": {"version": "1.0"},
            }],
            "version": 2,
        })
        path = _write_fixture(body)
        try:
            entries = agent._parse_package_resolved(path)
        finally:
            os.unlink(path)
        self.assertNotIn('url', entries[0])


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

    def test_url_preference_grafts_url_onto_first_seen_entry(self):
        # The bite the field-wise merge closes. A CocoaPods entry
        # (no url, has parents) comes first; an SPM entry (with
        # url) comes second. Pre-fix the merger ignored later
        # sources entirely and the upstream URL was lost — OSV
        # vuln lookups silently lost coverage. Post-fix the SPM
        # url is grafted onto the existing CocoaPods entry,
        # preserving the parent edges.
        umbrella = self._entry('Braintree')
        cocoapods_child = self._entry('Braintree/Core',
                                      parents=[umbrella['id']])
        cocoapods_child['direct'] = False
        spm_entry = self._entry('Braintree/Core')
        spm_entry['url'] = 'https://github.com/braintree/braintree_ios.git'
        merged, _ = agent._merge_dep_entries(
            [umbrella, cocoapods_child],
            [spm_entry],
        )
        core = next(e for e in merged if e['name'] == 'Braintree/Core')
        self.assertEqual(
            core.get('url'),
            'https://github.com/braintree/braintree_ios.git',
            'SPM url must graft onto the existing CocoaPods entry',
        )
        # Parents from CocoaPods survive — the load-bearing graph
        # signal the dashboard's deps tree view relies on.
        self.assertEqual(core['parents'], [umbrella['id']])
        # direct is OR-merged. CocoaPods said False (transitive),
        # SPM said True → result is True (it IS reachable
        # directly via SPM).
        self.assertTrue(core['direct'])

    def test_url_preference_does_not_demote_existing_direct_true(self):
        # Reverse asymmetry: SPM-style entry first (direct=true),
        # then a CocoaPods transitive (direct=false) bringing
        # parents. We pick up the parents but do NOT demote direct
        # from true → false. (`direct: true` is monotonic.)
        umbrella = self._entry('Umbrella')
        spm_first = self._entry('Pkg')
        spm_first['url'] = 'https://example.com/pkg.git'
        cocoapods_child = self._entry('Pkg', parents=[umbrella['id']])
        cocoapods_child['direct'] = False
        merged, _ = agent._merge_dep_entries(
            [spm_first],
            [umbrella, cocoapods_child],
        )
        pkg = next(e for e in merged if e['name'] == 'Pkg')
        self.assertTrue(pkg['direct'],
                        'first-source direct=True must not be demoted')
        # Empty previous parents → backfilled from incoming.
        self.assertEqual(pkg['parents'], [umbrella['id']])

    def test_url_preference_does_not_overwrite_existing_url(self):
        # When BOTH colliding entries have a url, first-seen wins
        # (no preference reason to replace).
        a_first = self._entry('A')
        a_first['url'] = 'https://first.example/A.git'
        a_second = self._entry('A')
        a_second['url'] = 'https://second.example/A.git'
        merged, _ = agent._merge_dep_entries([a_first], [a_second])
        self.assertEqual(merged[0]['url'], 'https://first.example/A.git')

    def test_url_preference_fills_missing_version(self):
        # Vendored frameworks emit `version: None`; SPM provides a
        # version. Field-wise merge should backfill the version
        # without affecting the existing url-less entry's other
        # fields.
        vendored = self._entry('Foo')
        vendored['version'] = None
        spm = self._entry('Foo')
        spm['version'] = '2.0'
        spm['url'] = 'https://example.com/foo.git'
        merged, _ = agent._merge_dep_entries([vendored], [spm])
        self.assertEqual(merged[0]['version'], '2.0')
        self.assertEqual(merged[0]['url'], 'https://example.com/foo.git')


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

    def test_blob_preserves_url_when_entry_carries_one(self):
        # Cross-language wire-shape pin. CLI-on path → entry dict
        # has `url`; pre-fix, the per-entry whitelist at this site
        # silently dropped url before gzip → SPM vuln-scan coverage
        # permanently degraded vs the SDK-side blob shape (which
        # uses `dependencies: entries` verbatim and preserves url).
        with_url = self._entry('Alamofire')
        with_url['url'] = 'https://github.com/Alamofire/Alamofire.git'
        without_url = self._entry('SocketRocket')
        _, blob = agent._build_dependencies_payload(
            [with_url, without_url], truncated=False, scope_label='all',
            clock=lambda: 0,
        )
        deps = {d['name']: d for d in blob['dependencies']}
        # Present on the entry → must round-trip into the blob.
        self.assertEqual(
            deps['Alamofire'].get('url'),
            'https://github.com/Alamofire/Alamofire.git',
        )
        # Absent on the entry → must NOT appear as `url: None`.
        # Mirrors the Rust DepEntry's
        # `#[serde(skip_serializing_if = "Option::is_none")]`.
        self.assertNotIn('url', deps['SocketRocket'])


# ──────────────────────────────────────────────
# Gzip / wire serialisation
# ──────────────────────────────────────────────

class TestPackageAppAsIpa(unittest.TestCase):
    """Pin the byte-deterministic synthetic .ipa packaging.

    The back-end dedupes re-uploads by content sha; two runs over the
    same source tree MUST produce hash-identical output, otherwise
    the dedup misses and storage costs balloon. Mirrors the SDK
    BugseeAgent's `TestPackageAppAsIpa` so both producers stay
    byte-equivalent."""

    def _make_app(self):
        tmp = tempfile.mkdtemp()
        app = os.path.join(tmp, 'Foo.app')
        os.makedirs(app)
        # A plist-shaped Info.plist (DEFLATEd in the archive).
        with open(os.path.join(app, 'Info.plist'), 'w') as f:
            f.write('<plist><dict><key>X</key><string>Y</string></dict></plist>')
        # A pre-compressed asset (STOREd).
        with open(os.path.join(app, 'icon.png'), 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32)
        # An executable Mach-O stub — POSIX exec bits must survive.
        exe = os.path.join(app, 'Foo')
        with open(exe, 'wb') as f:
            f.write(b'macho-stub')
        os.chmod(exe, 0o755)
        # A nested directory to exercise os.walk ordering.
        sub = os.path.join(app, 'Frameworks', 'B.framework')
        os.makedirs(sub)
        with open(os.path.join(sub, 'B'), 'wb') as f:
            f.write(b'b-binary')
        return tmp, app

    def test_produces_payload_prefix_arcnames(self):
        # Every entry must be under `Payload/<App>.app/...`. Pre-fix
        # a stray `os.path.join('Payload', ...)` on the wrong relpath
        # would land entries at the archive root, breaking the
        # back-end's `_analyze_bundle` walk.
        tmp, app = self._make_app()
        try:
            ipa = os.path.join(tmp, 'Foo.ipa')
            agent.package_app_as_ipa(app, ipa)
            with zipfile.ZipFile(ipa, 'r') as zf:
                names = zf.namelist()
            self.assertTrue(names, 'archive must not be empty')
            for n in names:
                self.assertTrue(
                    n.startswith('Payload/Foo.app/'),
                    'entry outside Payload/<App>.app/: %r' % n,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_is_byte_deterministic_across_runs(self):
        # Two archives of the same source tree must hash identically.
        # Catches: filesystem-order non-determinism, current-time
        # mtimes, or any other accidental injection of run-state.
        tmp, app = self._make_app()
        try:
            ipa_a = os.path.join(tmp, 'a.ipa')
            ipa_b = os.path.join(tmp, 'b.ipa')
            agent.package_app_as_ipa(app, ipa_a)
            agent.package_app_as_ipa(app, ipa_b)
            with open(ipa_a, 'rb') as fa, open(ipa_b, 'rb') as fb:
                self.assertEqual(
                    fa.read(), fb.read(),
                    'package_app_as_ipa must produce byte-identical output',
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skips_symlinks(self):
        # Real IPAs don't carry symlinks; zipfile can't encode them
        # without using non-standard extra fields. Skip them.
        tmp, app = self._make_app()
        try:
            link_target = os.path.join(app, 'Info.plist')
            link_src = os.path.join(app, 'InfoLink.plist')
            os.symlink(link_target, link_src)
            ipa = os.path.join(tmp, 'Foo.ipa')
            agent.package_app_as_ipa(app, ipa)
            with zipfile.ZipFile(ipa, 'r') as zf:
                names = zf.namelist()
            self.assertNotIn('Payload/Foo.app/InfoLink.plist', names,
                             'symlink must be skipped, got: %r' % names)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uses_store_for_already_compressed_extensions(self):
        # PNG / MP4 / etc. are pre-compressed; recompressing wastes
        # CPU without saving bytes. Pin per-entry compression method.
        tmp, app = self._make_app()
        try:
            ipa = os.path.join(tmp, 'Foo.ipa')
            agent.package_app_as_ipa(app, ipa)
            with zipfile.ZipFile(ipa, 'r') as zf:
                by_name = {i.filename: i for i in zf.infolist()}
            png = by_name.get('Payload/Foo.app/icon.png')
            self.assertIsNotNone(png, 'icon.png missing from archive')
            self.assertEqual(
                png.compress_type, zipfile.ZIP_STORED,
                'pre-compressed PNG must use STORE, not DEFLATE',
            )
            # Plain plist still uses DEFLATE — the negative-pin
            # companion to the STORE pin above.
            plist = by_name.get('Payload/Foo.app/Info.plist')
            self.assertIsNotNone(plist)
            self.assertEqual(plist.compress_type, zipfile.ZIP_DEFLATED)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preserves_executable_bits_on_mach_o(self):
        # The back-end's "is this the main binary?" heuristic uses
        # the POSIX exec bits stored in `external_attr`. Without
        # preservation, every embedded executable encodes as plain
        # data and the heuristic loses signal.
        tmp, app = self._make_app()
        try:
            ipa = os.path.join(tmp, 'Foo.ipa')
            agent.package_app_as_ipa(app, ipa)
            with zipfile.ZipFile(ipa, 'r') as zf:
                by_name = {i.filename: i for i in zf.infolist()}
            exe = by_name.get('Payload/Foo.app/Foo')
            self.assertIsNotNone(exe, 'main exe missing from archive')
            mode = (exe.external_attr >> 16) & 0xFFFF
            self.assertTrue(
                mode & 0o100,
                'owner-exec bit must survive; got mode=%o' % mode,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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

    def test_coerces_non_dict_cli_return_to_empty_dict(self):
        # The CLI's `build-env read-plist` returns whatever
        # `json.loads(stdout)` produces with no shape validation.
        # A future CLI bug or version skew could surface as a JSON
        # array / string / number — every caller indexes with
        # `.get(...)` and would crash with AttributeError if the
        # coercion didn't fire at the choke point. Pin: non-dict
        # CLI returns flatten to {} so callers are uniformly safe.
        for non_dict in [[1, 2, 3], "string", 42, None]:
            with mock.patch.object(
                agent, '_read_info_plist_via_cli',
                return_value=non_dict,
            ):
                result = agent._read_info_plist('/some/path.plist')
            self.assertEqual(
                result, {},
                "non-dict CLI return %r must coerce to {}" % (non_dict,),
            )

    def test_coerces_non_dict_plistlib_return_to_empty_dict(self):
        # plistlib can parse top-level NSArray plists into a list
        # (rare but legal). Same coercion as the CLI path — the
        # callers want a dict-or-{} interface.
        import plistlib
        fd, path = tempfile.mkstemp(suffix='.plist')
        with os.fdopen(fd, 'wb') as f:
            plistlib.dump(['arr', 'top', 'level'], f)
        try:
            # Force the plistlib branch (CLI returns None).
            with mock.patch.object(
                agent, '_read_info_plist_via_cli', return_value=None,
            ):
                result = agent._read_info_plist(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, {})


# ──────────────────────────────────────────────
# _find_first_above
# ──────────────────────────────────────────────

class TestExtractUuidFromApp(unittest.TestCase):
    """Pin the arch-cascade + format normalisation contract for
    `_extract_uuid_from_app`. Mirrors the SDK BugseeAgent's
    `test_bugsee_agent_dsym_cli.py::TestGetMainExecutableUuid` posture
    so both producers stay byte-identical for the same fat-binary
    build."""

    def _make_app(self, executable_name='Foo'):
        tmp = tempfile.mkdtemp()
        app = os.path.join(tmp, 'Foo.app')
        os.makedirs(app)
        import plistlib
        with open(os.path.join(app, 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleExecutable': executable_name}, f)
        with open(os.path.join(app, executable_name), 'wb') as f:
            f.write(b'macho-stub')
        return tmp, app

    def test_picks_arm64_slice_from_fat_binary(self):
        # Three slices: arm64, x86_64, arm64-simulator. arm64 wins
        # per _PREFERRED_MACHO_ARCHS cascade.
        tmp, app = self._make_app()
        try:
            slices = {
                'x86_64':           'BB00000000000000000000000000000011',
                'arm64-simulator':  'CC00000000000000000000000000000022',
                'arm64':            'AA00000000000000000000000000000033',
            }
            with mock.patch.object(
                agent, '_load_macho_slices_via_cli',
                return_value=slices,
            ):
                uuid_str = agent._extract_uuid_from_app(app)
            # Lowercase no-dashes — normalised at the cascade exit.
            self.assertEqual(
                uuid_str, 'aa00000000000000000000000000000033',
                "arm64 slice must win over x86_64 / simulator",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_falls_back_to_first_slice_when_no_preferred_arch(self):
        # Only an exotic arch present (e.g. riscv64 — not in the
        # _PREFERRED_MACHO_ARCHS list). Should pick the first
        # reported by the CLI (dict-insertion-order in Python 3.7+).
        tmp, app = self._make_app()
        try:
            slices = {
                'riscv64': 'DD00000000000000000000000000000044',
            }
            with mock.patch.object(
                agent, '_load_macho_slices_via_cli',
                return_value=slices,
            ):
                uuid_str = agent._extract_uuid_from_app(app)
            self.assertEqual(uuid_str, 'dd00000000000000000000000000000044')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_falls_back_to_flat_dsym_uuid_when_slices_returns_none(self):
        # Older bugsee-cli without the `dsym slices` subcommand.
        # Slices helper returns None; flat-uuid helper supplies the
        # first UUID; we normalise it.
        tmp, app = self._make_app()
        try:
            with mock.patch.object(
                agent, '_load_macho_slices_via_cli',
                return_value=None,
            ), mock.patch.object(
                agent, '_parse_dsym_via_cli',
                return_value=['EE000000-0000-0000-0000-000000000055'],
            ):
                uuid_str = agent._extract_uuid_from_app(app)
            # Lowercase, no dashes, exactly 32 chars — the normalised
            # canonical shape.
            self.assertEqual(uuid_str, 'ee000000000000000000000000000055')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_returns_none_when_executable_missing_from_plist(self):
        tmp = tempfile.mkdtemp()
        try:
            app = os.path.join(tmp, 'Foo.app')
            os.makedirs(app)
            import plistlib
            with open(os.path.join(app, 'Info.plist'), 'wb') as f:
                plistlib.dump({'CFBundleIdentifier': 'com.example'}, f)
            # No CFBundleExecutable → None.
            self.assertIsNone(agent._extract_uuid_from_app(app))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalisation_strips_whitespace_and_rejects_empty(self):
        # The whitespace-only guard. Defensive against a future CLI
        # bug emitting `"  "` or `"-"` for an unparseable Mach-O.
        tmp, app = self._make_app()
        try:
            for raw in ["  ", "-", "---", ""]:
                with mock.patch.object(
                    agent, '_load_macho_slices_via_cli',
                    return_value={'arm64': raw},
                ):
                    self.assertIsNone(
                        agent._extract_uuid_from_app(app),
                        "whitespace-only / dashes-only %r must produce None"
                        % (raw,),
                    )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNormaliseBuildUuid(unittest.TestCase):
    """Pin the canonical 32-char lowercase-no-dash shape every cascade
    exit funnels through. Catches a regression in any of the three
    cascade fallbacks (Mach-O LC_UUID, dwarfdump dSYM scan,
    uuid.uuid4)."""

    def test_uppercase_hyphenated_input_normalises(self):
        # The shape `dwarfdump -u` historically emits.
        self.assertEqual(
            agent._normalise_build_uuid('54D75FB3-747F-387F-8A93-4EA034B1F8CF'),
            '54d75fb3747f387f8a934ea034b1f8cf',
        )

    def test_lowercase_hyphenated_input_normalises(self):
        # The shape `uuid.uuid4()` returns.
        self.assertEqual(
            agent._normalise_build_uuid('aaaa1111-bbbb-2222-cccc-333344445555'),
            'aaaa1111bbbb2222cccc333344445555',
        )

    def test_already_normalised_input_round_trips_unchanged(self):
        # Step 1 (Mach-O LC_UUID via `_extract_uuid_from_app`)
        # already emits the canonical shape — re-normalising must
        # be idempotent.
        canonical = 'aa00000000000000000000000000000033'
        self.assertEqual(
            agent._normalise_build_uuid(canonical), canonical,
        )

    def test_whitespace_only_input_returns_none(self):
        for raw in ["", "  ", "-", "----", None]:
            self.assertIsNone(
                agent._normalise_build_uuid(raw),
                "whitespace-only %r must coerce to None" % (raw,),
            )

    def test_emitted_shape_is_32_lowercase_hex(self):
        # Output contract: exactly 32 characters, all in
        # `[0-9a-f]`. Catches a mutation that switched to uppercase
        # or kept dashes.
        import re
        for raw in [
            '54D75FB3-747F-387F-8A93-4EA034B1F8CF',
            'aaaa1111-bbbb-2222-cccc-333344445555',
            'ZZ' * 16,  # technically not hex but shape pin
        ]:
            out = agent._normalise_build_uuid(raw)
            if out is None:
                continue
            self.assertEqual(len(out), 32, "len(%r) != 32" % out)
            self.assertEqual(
                out, out.lower(),
                "%r contains uppercase chars" % out,
            )
            self.assertNotIn('-', out, "%r still contains dashes" % out)


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
            # Normalised via `_normalise_build_uuid` on the way out
            # (lifted from the per-callsite normalisation that used
            # to live in run_artifact_upload_flow). Catches a
            # regression that bypassed the canonical-shape contract.
            self.assertEqual(uuid, 'abcdef1234567890abcdef1234567890')

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


class TestCollectAllDependenciesViaCli(unittest.TestCase):
    """Pins the Option-C migration of iOS deps collection to
    bugsee-cli (the Rust subcommand `bugsee-cli ios-deps collect`).
    The Python parsers in `_collect_all_dependencies` MUST remain a
    usable fallback for environments where the CLI isn't available
    — but when the CLI IS available, its JSON output MUST be
    honored verbatim (one cross-language source of truth)."""

    def _stub_run(self, returncode=0, stdout='{}'):
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    def test_uses_cli_output_when_cli_is_available(self):
        # resolveCli returns a path → CLI invoked → JSON parsed →
        # returned as the canonical tuple. Python parsers MUST
        # NOT fire.
        canned = '{"entries":[{"id":"library::Foo","name":"Foo",' \
                 '"version":"1.0","direct":true,"type":"library",' \
                 '"group":"","parents":[]}],' \
                 '"scope_label":"all","truncated":false}'
        with mock.patch.object(agent, 'resolveCli',
                                  return_value='/path/to/bugsee-cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout=canned)) as run_mock:
            entries, scope, truncated = agent._collect_all_dependencies(
                '/some/dir',
            )
        # The CLI subcommand's entries win — no Python parsing.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'Foo')
        self.assertEqual(scope, 'all')
        self.assertFalse(truncated)
        # subprocess.run called once with the correct argv shape.
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[0], '/path/to/bugsee-cli')
        self.assertIn('ios-deps', argv)
        self.assertIn('collect', argv)
        pr_idx = argv.index('--project-root')
        self.assertEqual(argv[pr_idx + 1], '/some/dir')

    def test_forwards_product_binary_when_present(self):
        # When a product binary is resolved, the CLI invocation
        # MUST forward it via --product-binary so the vendored-
        # framework scan happens Rust-side.
        canned = '{"entries":[],"scope_label":"all","truncated":false}'
        with mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value='/path/to/MyApp'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout=canned)) as run_mock:
            agent._collect_all_dependencies('/some/dir')
        argv = run_mock.call_args[0][0]
        pb_idx = argv.index('--product-binary')
        self.assertEqual(argv[pb_idx + 1], '/path/to/MyApp')

    def test_omits_product_binary_when_not_resolved(self):
        # No product binary → no flag. The CLI's signature is
        # `--product-binary` optional; passing nothing is the
        # correct shape.
        canned = '{"entries":[],"scope_label":"all","truncated":false}'
        with mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout=canned)) as run_mock:
            agent._collect_all_dependencies('/some/dir')
        argv = run_mock.call_args[0][0]
        self.assertNotIn('--product-binary', argv)

    def test_falls_back_to_python_when_cli_unavailable(self):
        # resolveCli returns None → Python parsers run. Verify by
        # supplying a real Podfile.lock and asserting the Python
        # parser's specific output (1 entry, version 0.7.1).
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(agent, 'resolveCli',
                                  return_value=None), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run') as run_mock:
            podfile = os.path.join(root, 'Podfile.lock')
            with open(podfile, 'w') as f:
                f.write("PODS:\n  - SocketRocket (0.7.1)\n\n"
                        "DEPENDENCIES:\n  - SocketRocket\n\n"
                        "SPEC CHECKSUMS:\n  SocketRocket: feedface\n")
            entries, scope, truncated = agent._collect_all_dependencies(root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'SocketRocket')
        self.assertEqual(entries[0]['version'], '0.7.1')
        # subprocess.run NEVER called (no CLI to shell to).
        self.assertEqual(run_mock.call_count, 0)

    def test_falls_back_to_python_on_cli_nonzero_exit(self):
        # CLI exits non-zero (older bugsee-cli without the
        # subcommand → exit 2). Fall through to Python parsers.
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=2, stdout='')):
            # Same fixture as above so the assertion can pin the
            # Python parser's output, NOT a CLI canned response.
            podfile = os.path.join(root, 'Podfile.lock')
            with open(podfile, 'w') as f:
                f.write("PODS:\n  - SocketRocket (0.7.1)\n\n"
                        "DEPENDENCIES:\n  - SocketRocket\n\n"
                        "SPEC CHECKSUMS:\n  SocketRocket: feedface\n")
            entries, _, _ = agent._collect_all_dependencies(root)
        self.assertEqual(entries[0]['name'], 'SocketRocket')

    def test_falls_back_to_python_on_malformed_cli_json(self):
        # CLI emits garbage / partial stream → ValueError → fall
        # back to Python.
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='not json')):
            podfile = os.path.join(root, 'Podfile.lock')
            with open(podfile, 'w') as f:
                f.write("PODS:\n  - SocketRocket (0.7.1)\n\n"
                        "DEPENDENCIES:\n  - SocketRocket\n\n"
                        "SPEC CHECKSUMS:\n  SocketRocket: feedface\n")
            entries, _, _ = agent._collect_all_dependencies(root)
        self.assertEqual(entries[0]['name'], 'SocketRocket')

    def test_falls_back_to_python_on_oserror(self):
        # ENOEXEC / FileNotFoundError on a stale CLI path → OSError
        # → fall back to Python. Same posture as the dSYM upload
        # path and the VCS metadata fallback.
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent, '_resolve_product_binary_path',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run',
                                  side_effect=OSError("ENOEXEC")):
            podfile = os.path.join(root, 'Podfile.lock')
            with open(podfile, 'w') as f:
                f.write("PODS:\n  - SocketRocket (0.7.1)\n\n"
                        "DEPENDENCIES:\n  - SocketRocket\n\n"
                        "SPEC CHECKSUMS:\n  SocketRocket: feedface\n")
            entries, _, _ = agent._collect_all_dependencies(root)
        self.assertEqual(entries[0]['name'], 'SocketRocket')


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

    def test_github_actions_tag_push_omits_branch(self):
        # Tag push: GITHUB_REF=refs/tags/<tag>, EVENT=push. Pre-fix
        # the `.replace('refs/heads/', '', 1)` left the ref unchanged
        # (`refs/tags/v1.0.0`) and the dashboard rendered the literal
        # string in the branch column. Post-fix: only emit `branch`
        # when the ref starts with `refs/heads/`. Matches the Android
        # Gradle plugin's canonical Kotlin resolver and the Rust CLI.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS':     'true',
            'GITHUB_SHA':         'tag-sha-abc',
            'GITHUB_REPOSITORY':  'org/repo',
            'GITHUB_REF':         'refs/tags/v1.0.0',
            'GITHUB_EVENT_NAME':  'push',
        }, clear=True):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'],   'github')
        self.assertEqual(vcs['commit_sha'], 'tag-sha-abc')
        # branch must NOT appear — neither as literal ref string nor
        # as an empty value. The canonical contract is omission.
        self.assertNotIn(
            'branch', vcs,
            "tag-pushed branch must be omitted, not the literal ref"
        )

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

    def test_gitlab_tag_pipeline_omits_branch(self):
        # Tag pipelines set CI_COMMIT_TAG (not CI_COMMIT_BRANCH) +
        # CI_COMMIT_REF_NAME (the tag name). The historic fallback
        # leaked the tag into the branch column. Post-fix: branch
        # must be absent on tag pipelines.
        #
        # Force the Python in-process resolver path with
        # `resolveCli=None` so a bugsee-cli on the test runner's
        # PATH doesn't shadow the gate we're actually trying to pin.
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':          'true',
            'CI_COMMIT_SHA':      'gl-tag-sha',
            'CI_COMMIT_TAG':      'v1.0.0',
            'CI_COMMIT_REF_NAME': 'v1.0.0',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(vcs['provider'], 'gitlab')
        self.assertNotIn(
            'branch', vcs,
            "tag pipeline must omit `branch`, not echo the tag name",
        )

    def test_gitlab_branch_pipeline_prefers_ci_commit_branch(self):
        # Modern GitLab (>=12.6) sets both CI_COMMIT_BRANCH and
        # CI_COMMIT_REF_NAME on branch pipelines. The gate must
        # prefer CI_COMMIT_BRANCH over the ref-name fallback. Use
        # distinct sentinel values so a mutation that flipped the
        # preference order is observable; force the Python path with
        # `resolveCli=None`.
        with mock.patch.dict(os.environ, {
            'GITLAB_CI':          'true',
            'CI_COMMIT_SHA':      'gl-branch-sha',
            'CI_COMMIT_BRANCH':   'feature/x',
            'CI_COMMIT_REF_NAME': 'from-ref-name-sentinel',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None):
            vcs = agent.resolve_vcs_metadata('/no/working/dir')
        self.assertEqual(
            vcs['branch'], 'feature/x',
            "gate must prefer CI_COMMIT_BRANCH; got the ref-name sentinel"
            " instead — preference order regressed",
        )

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


class TestResolveVcsMetadataViaCli(unittest.TestCase):
    """Pins the Option-C migration of VCS resolution to bugsee-cli
    (the Rust subcommand `bugsee-cli vcs-metadata`). The Python
    fallback in `resolve_vcs_metadata` MUST remain a usable
    fallback for environments where the CLI isn't available — but
    when the CLI IS available, its JSON output MUST be honored
    verbatim (one cross-language source of truth)."""

    def _stub_run(self, returncode=0, stdout='{}'):
        # subprocess.run mock that mimics the CompletedProcess
        # shape the production code accesses (.returncode +
        # .stdout). Defaults to a successful empty-object run.
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    def test_uses_cli_output_when_cli_is_available(self):
        # Production code: resolveCli returns a path → CLI is
        # invoked → JSON parsed → returned directly. The Python
        # fallback below MUST NOT fire.
        canned = '{"provider":"github","commit_sha":"from-cli",' \
                 '"branch":"main","repo":"org/repo"}'
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/path/to/bugsee-cli') as cli_mock, \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout=canned)) as run_mock:
            vcs = agent.resolve_vcs_metadata('/some/dir')
        # The CLI subcommand's JSON wins — provider and
        # commit_sha must match what the CLI returned, NOT what
        # the Python fallback would have synthesized.
        self.assertEqual(vcs['provider'],   'github')
        self.assertEqual(vcs['commit_sha'], 'from-cli')
        # resolveCli was consulted; subprocess.run was invoked
        # with the vcs-metadata subcommand and the working-dir.
        cli_mock.assert_called_once()
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[0], '/path/to/bugsee-cli')
        self.assertIn('vcs-metadata', argv)
        wd_idx = argv.index('--working-dir')
        self.assertEqual(argv[wd_idx + 1], '/some/dir')

    def test_falls_back_to_python_when_cli_unavailable(self):
        # resolveCli returns None (download failed, unsupported
        # host triple, etc.) → Python fallback fires. Asserted
        # by setting GITHUB_ACTIONS env so the Python fallback
        # has something to return.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'GITHUB_SHA':     'python-sha',
            'GITHUB_REF':     'refs/heads/master',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run') as run_mock:
            vcs = agent.resolve_vcs_metadata('/no/dir')
        self.assertEqual(vcs['provider'],   'github')
        self.assertEqual(vcs['commit_sha'], 'python-sha')
        # No subprocess.run call attempted — Python only.
        self.assertEqual(run_mock.call_count, 0)

    def test_falls_back_to_python_on_cli_nonzero_exit(self):
        # CLI exits non-zero (e.g. older bugsee-cli without the
        # vcs-metadata subcommand → clap prints "error: unknown
        # command" and exits 2). MUST fall back rather than
        # bubble the failure.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'GITHUB_SHA':     'fallback-sha',
            'GITHUB_REF':     'refs/heads/main',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=2, stdout='')):
            vcs = agent.resolve_vcs_metadata('/no/dir')
        self.assertEqual(vcs['commit_sha'], 'fallback-sha')

    def test_falls_back_to_python_on_malformed_cli_json(self):
        # If the CLI prints garbage (or a partial stream), the
        # Python json.loads raises ValueError — we treat this as
        # CLI unavailable and fall back.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'GITHUB_SHA':     'fallback-sha',
            'GITHUB_REF':     'refs/heads/main',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='not json')):
            vcs = agent.resolve_vcs_metadata('/no/dir')
        self.assertEqual(vcs['commit_sha'], 'fallback-sha')

    def test_falls_back_to_python_on_oserror(self):
        # ENOEXEC / FileNotFoundError on a stale CLI path → OSError
        # → fall back. Same posture as the dSYM upload path.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'GITHUB_SHA':     'fallback-sha',
            'GITHUB_REF':     'refs/heads/main',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  side_effect=OSError("ENOEXEC")):
            vcs = agent.resolve_vcs_metadata('/no/dir')
        self.assertEqual(vcs['commit_sha'], 'fallback-sha')

    def test_uses_working_dir_default_when_empty(self):
        # `working_dir` empty string → CLI invoked with "." so it
        # falls back to the CWD rather than the literal empty
        # string (which `git -C ''` would reject).
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='{}')) as run_mock:
            agent.resolve_vcs_metadata('')
        argv = run_mock.call_args[0][0]
        wd_idx = argv.index('--working-dir')
        self.assertEqual(argv[wd_idx + 1], '.')


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
        #
        # NB: the production resolver also tries `bugsee-cli build-env
        # xcode-version` first; we skip that path here by mocking
        # resolveCli to return None so the Python xcodebuild
        # fallback gets exercised. The CLI path has its own tests
        # in TestResolveXcodeVersionViaCli (TBD) / on the Rust side.
        canned = SimpleNamespace(
            returncode=0,
            stdout="Xcode 16.0\nBuild version 16A242d\n",
        )
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None), \
                mock.patch('subprocess.run', return_value=canned):
            self.assertEqual(agent.resolve_xcode_version(), '16.0')

    def test_returns_none_when_xcodebuild_fails(self):
        # `xcodebuild -version` nonzero exit → resolver returns
        # None so the caller can omit the field rather than
        # surfacing a garbled value.
        canned = SimpleNamespace(returncode=1, stdout='', stderr='nope')
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None), \
                mock.patch('subprocess.run', return_value=canned):
            self.assertIsNone(agent.resolve_xcode_version())

    def test_returns_none_when_xcodebuild_missing(self):
        # FileNotFoundError on stripped-down CI images without
        # Xcode installed.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None), \
                mock.patch('subprocess.run',
                           side_effect=FileNotFoundError("xcodebuild")):
            self.assertIsNone(agent.resolve_xcode_version())

    def test_non_numeric_env_var_falls_through(self):
        # Defensive: if Xcode ever exports a dotted form directly
        # (`16.2.0`), the `.isdigit()` guard must short-circuit
        # the numeric reformat and fall through to xcodebuild.
        # Same as above — mock the CLI path to None to exercise
        # the Python fallback specifically.
        canned = SimpleNamespace(
            returncode=0, stdout="Xcode 16.2.0\n",
        )
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '16.2.0'},
                             clear=True), \
                mock.patch.object(agent, 'resolveCli', return_value=None), \
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


# ──────────────────────────────────────────────
# Build timings (xcactivitylog)
# ──────────────────────────────────────────────
#
# Ported from the iOS SDK's BugseeAgent. The full SLF tokenizer +
# section extractor is exercised end-to-end by feeding real
# xcactivitylog files through resolve_build_timings during
# integration testing; here we cover the units that are easy to
# fixture without a real Xcode log:
#
#   - _classify_section_title — the title-pattern table that maps
#     Xcode build-section names to (managed_code / native /
#     resources / packaging / other) categories.
#   - resolve_build_timings soft-fail paths — empty env / missing
#     OBJROOT / no log directory must degrade to (None, None)
#     rather than raising, since a timings failure must not break
#     the parent pipeline.
#   - _find_derived_data_root / _find_latest_xcactivitylog —
#     filesystem walk over a fake DerivedData layout.


class TestClassifySectionTitle(unittest.TestCase):
    """Pins the iOS-side category-classification rules. The
    output ({managed_code, native, resources, packaging, other,
    None}) drives the wire-format category_sums block — drift
    here would silently re-bucket every iOS build's timings on the
    dashboard."""

    # NB: the classifier expects raw Xcode section titles. The
    # function returns the category string OR None for sections
    # that should NOT contribute to any category (typically wrapper
    # / container sections that would otherwise double-count).
    def _classify(self, title):
        return agent._classify_section_title(title)

    def test_swift_compile_classified_as_native(self):
        # Per-source Swift compile event — the dominant unit on
        # most iOS builds. Drift to anything else would re-bucket
        # the largest chunk of every build's timings.
        self.assertEqual(self._classify("Compile Foo.swift (arm64)"),
                         "native")

    def test_objc_compile_classified_as_native(self):
        # Obj-C remains relevant on bridging-header projects and
        # legacy targets. Same bucket as Swift since both produce
        # native code.
        self.assertEqual(self._classify("Compile Bar.m (arm64)"),
                         "native")

    def test_compile_swift_sources_phase_classified_as_native(self):
        # Aggregate phase name from `swiftc`'s build plan.
        self.assertEqual(self._classify("CompileSwiftSources"),
                         "native")

    def test_compile_clang_module_classified_as_native(self):
        # Cross-cuts the `^Compiling ` generic wrapper-skip rule
        # — pinned in the SDK's source as a precedence carve-out.
        # If the carve-out is removed, this falls through to None
        # and the dashboard loses Clang-module compile time.
        self.assertEqual(self._classify("Compiling Clang module Foundation"),
                         "native")

    def test_asset_catalog_classified_as_resources(self):
        self.assertEqual(self._classify("CompileAssetCatalog"),
                         "resources")

    def test_storyboard_classified_as_resources(self):
        self.assertEqual(self._classify("CompileStoryboard"),
                         "resources")

    def test_info_plist_processing_classified_as_resources(self):
        # Plist processing is bucketed with other "build inputs"
        # not with linking/packaging.
        self.assertEqual(self._classify("ProcessInfoPlistFile"),
                         "resources")

    def test_link_storyboards_classified_as_resources(self):
        # PRECEDENCE PIN: `LinkStoryboards` MUST be classified as
        # resources, not packaging. The packaging tuple has a
        # generic `^Link\b` rule that would otherwise claim it.
        # The classifier checks native → resources → packaging in
        # order — a reordering of those three groups would cause
        # this test to fail clearly rather than silently
        # mis-bucketing storyboard linking.
        self.assertEqual(self._classify("LinkStoryboards"),
                         "resources")

    def test_link_binary_classified_as_packaging(self):
        # The actual `Link MyApp.app/MyApp normal arm64` step.
        self.assertEqual(self._classify("Link Foo.app/Foo normal arm64"),
                         "packaging")

    def test_codesign_classified_as_packaging(self):
        self.assertEqual(self._classify("CodeSign Foo.app"),
                         "packaging")

    def test_dsym_generation_classified_as_packaging(self):
        self.assertEqual(self._classify("GenerateDSYMFile"),
                         "packaging")

    def test_swift_stdlib_embed_classified_as_packaging(self):
        # Embedding the Swift runtime dylibs into the app bundle
        # is conceptually packaging, not compilation. A regression
        # that moved this back to `native` would skew "where did
        # the time go" reporting.
        self.assertEqual(self._classify("Copy Swift standard libraries"),
                         "packaging")


class TestResolveBuildTimingsSoftFail(unittest.TestCase):
    """The pipeline expects (None, None) when timings are
    unavailable — not an exception. Pin the soft-fail contract on
    the common "nothing to find" paths."""

    def test_empty_env_returns_none_none(self):
        # Pristine env, no OBJROOT — the whole pipeline must
        # degrade silently rather than raising.
        summary, gz = agent.resolve_build_timings({})
        self.assertIsNone(summary)
        self.assertIsNone(gz)

    def test_objroot_pointing_at_nonexistent_dir_returns_none(self):
        summary, gz = agent.resolve_build_timings(
            {'OBJROOT': '/no/such/derived/data'}
        )
        self.assertIsNone(summary)
        self.assertIsNone(gz)

    def test_objroot_with_no_logs_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            # Realistic OBJROOT shape but missing the Logs/Build
            # subdirectory — the walk-up finds nothing.
            objroot = os.path.join(root, 'Build', 'Intermediates.noindex')
            os.makedirs(objroot)
            summary, gz = agent.resolve_build_timings({'OBJROOT': objroot})
            self.assertIsNone(summary)
            self.assertIsNone(gz)


class TestFindDerivedDataRoot(unittest.TestCase):
    """The walk-up locates the Xcode DerivedData root from any
    descendant under it. Pins the boundary conditions (max steps,
    no-match, top-down match)."""

    def test_returns_none_for_empty_arg(self):
        self.assertIsNone(agent._find_derived_data_root(None))
        self.assertIsNone(agent._find_derived_data_root(""))

    def test_walks_up_to_logs_build(self):
        with tempfile.TemporaryDirectory() as dd_root:
            # Construct a realistic DerivedData/Build/Intermediates/
            # ArchiveIntermediates layout.
            os.makedirs(os.path.join(dd_root, 'Logs', 'Build'))
            deep_obj_root = os.path.join(
                dd_root, 'Build', 'Intermediates.noindex',
                'ArchiveIntermediates', 'MyScheme',
                'IntermediateBuildFilesPath',
            )
            os.makedirs(deep_obj_root)

            found = agent._find_derived_data_root(deep_obj_root)
            # realpath both sides — macOS /tmp ↔ /private/tmp symlink
            # would otherwise make this test fail on the host but
            # pass in CI containers.
            self.assertEqual(os.path.realpath(found),
                             os.path.realpath(dd_root))

    def test_returns_none_when_logs_build_not_found_within_cap(self):
        # Construct a tree deeper than the 10-step cap; the walk
        # MUST stop and return None rather than continue to /.
        with tempfile.TemporaryDirectory() as root:
            deep = root
            for _ in range(12):
                deep = os.path.join(deep, 'x')
            os.makedirs(deep)
            # No Logs/Build anywhere in the chain.
            self.assertIsNone(agent._find_derived_data_root(deep))


class TestFindLatestXcactivitylog(unittest.TestCase):
    """The picker takes the newest-mtime log file; pin the tie-
    break behaviour on equal mtimes (descending filename)."""

    def test_returns_none_when_no_logs(self):
        with tempfile.TemporaryDirectory() as dd_root:
            os.makedirs(os.path.join(dd_root, 'Logs', 'Build'))
            objroot = os.path.join(dd_root, 'Build', 'Intermediates.noindex')
            os.makedirs(objroot)
            self.assertIsNone(agent._find_latest_xcactivitylog(objroot))

    def test_returns_newest_log_by_mtime(self):
        import time as _t
        with tempfile.TemporaryDirectory() as dd_root:
            logs = os.path.join(dd_root, 'Logs', 'Build')
            os.makedirs(logs)
            old = os.path.join(logs, 'AAAA.xcactivitylog')
            new = os.path.join(logs, 'BBBB.xcactivitylog')
            open(old, 'wb').close()
            _t.sleep(0.05)
            open(new, 'wb').close()
            objroot = os.path.join(dd_root, 'Build', 'Intermediates.noindex')
            os.makedirs(objroot)

            picked = agent._find_latest_xcactivitylog(objroot)
            self.assertEqual(os.path.realpath(picked),
                             os.path.realpath(new))

    def test_tie_break_descends_by_filename(self):
        # When mtimes are identical (HFS+ second-granularity, or
        # rsync-preserving), descending filename order MUST win.
        # The agent's UUID-based filenames have a monotonic prefix
        # so descending == newest.
        import os.path as _op
        with tempfile.TemporaryDirectory() as dd_root:
            logs = os.path.join(dd_root, 'Logs', 'Build')
            os.makedirs(logs)
            a = _op.join(logs, 'AAAA.xcactivitylog')
            z = _op.join(logs, 'ZZZZ.xcactivitylog')
            open(a, 'wb').close()
            open(z, 'wb').close()
            # Set identical mtimes (use os.utime).
            os.utime(a, (1_700_000_000, 1_700_000_000))
            os.utime(z, (1_700_000_000, 1_700_000_000))

            objroot = os.path.join(dd_root, 'Build', 'Intermediates.noindex')
            os.makedirs(objroot)
            picked = agent._find_latest_xcactivitylog(objroot)
            self.assertEqual(os.path.basename(picked), 'ZZZZ.xcactivitylog')


class TestBuildEnvViaCli(unittest.TestCase):
    """Pins the Option-C migration of three build-env helpers
    (xcode-version, machine-label, _read_info_plist) to the
    `bugsee-cli build-env` subcommands. Same migration pattern
    the VCS resolver and iOS deps parsers landed first — the CLI
    is the canonical implementation; each helper falls back to
    its in-process Python implementation only when the CLI isn't
    available."""

    def _stub_run(self, returncode=0, stdout=''):
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    # ── xcode-version ────────────────────────────────────────────

    def test_xcode_version_uses_cli_output_when_available(self):
        # CLI returns "16.2.0" → that's used directly. Python
        # fallback (which would parse XCODE_VERSION_ACTUAL or
        # shell to xcodebuild) MUST NOT fire.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/path/to/bugsee-cli') as cli_mock, \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='16.2.0\n')) as run_mock:
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')
        cli_mock.assert_called_once()
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[0], '/path/to/bugsee-cli')
        self.assertIn('build-env', argv)
        self.assertIn('xcode-version', argv)

    def test_xcode_version_falls_back_when_cli_unavailable(self):
        # resolveCli returns None → Python path runs. Verify by
        # setting XCODE_VERSION_ACTUAL so the Python branch
        # produces a known value.
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '1620'},
                             clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run') as run_mock:
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')
        # subprocess.run NEVER called — pure Python branch.
        self.assertEqual(run_mock.call_count, 0)

    def test_xcode_version_falls_back_on_empty_cli_stdout(self):
        # CLI prints empty (couldn't resolve) → fall back to
        # Python. The Python branch with XCODE_VERSION_ACTUAL set
        # produces "16.2.0" verbatim.
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '1620'},
                             clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='')):
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')

    def test_xcode_version_falls_back_on_nonzero_exit(self):
        with mock.patch.dict(os.environ,
                             {'XCODE_VERSION_ACTUAL': '1620'},
                             clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=2, stdout='')):
            self.assertEqual(agent.resolve_xcode_version(), '16.2.0')

    # ── machine-label ────────────────────────────────────────────

    def test_machine_label_uses_cli_output_when_available(self):
        with mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0,
                                      stdout='github-actions:runner-1\n')) as run_mock:
            self.assertEqual(agent.resolve_machine_label(),
                             'github-actions:runner-1')
        argv = run_mock.call_args[0][0]
        self.assertIn('machine-label', argv)

    def test_machine_label_falls_back_when_cli_unavailable(self):
        # CI env makes the Python fallback produce the same
        # github-actions label, so we can assert on it.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'RUNNER_NAME':    'py-runner',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value=None), \
                mock.patch.object(agent.subprocess, 'run') as run_mock:
            self.assertEqual(agent.resolve_machine_label(),
                             'github-actions:py-runner')
        self.assertEqual(run_mock.call_count, 0)

    def test_machine_label_falls_back_on_empty_cli_output(self):
        # CLI prints empty (sandboxed environment with no
        # hostname AND no CI provider) → fall back to Python.
        with mock.patch.dict(os.environ, {
            'GITHUB_ACTIONS': 'true',
            'RUNNER_NAME':    'py-runner',
        }, clear=True), \
                mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout='')):
            self.assertEqual(agent.resolve_machine_label(),
                             'github-actions:py-runner')

    # ── read-plist ───────────────────────────────────────────────

    def test_read_plist_uses_cli_output_when_available(self):
        canned = '{"CFBundleShortVersionString":"1.2.3",' \
                 '"CFBundleVersion":"42",' \
                 '"CFBundleIdentifier":"com.example.app"}'
        with mock.patch.object(agent, 'resolveCli',
                                  return_value='/cli'), \
                mock.patch.object(agent.subprocess, 'run',
                                  return_value=self._stub_run(
                                      returncode=0, stdout=canned)) as run_mock:
            result = agent._read_info_plist('/path/to/Info.plist')
        self.assertEqual(result['CFBundleShortVersionString'], '1.2.3')
        self.assertEqual(result['CFBundleVersion'], '42')
        self.assertEqual(result['CFBundleIdentifier'], 'com.example.app')
        argv = run_mock.call_args[0][0]
        self.assertIn('read-plist', argv)
        # The plist path is passed as the positional argument.
        self.assertEqual(argv[-1], '/path/to/Info.plist')

    def test_read_plist_falls_back_when_cli_unavailable(self):
        # resolveCli returns None → fall back to in-process
        # plistlib. Verify by supplying a real XML plist fixture.
        import plistlib
        with tempfile.NamedTemporaryFile(
                suffix='.plist', delete=False) as f:
            plistlib.dump({'CFBundleVersion': '99'}, f)
            path = f.name
        try:
            with mock.patch.object(agent, 'resolveCli',
                                      return_value=None), \
                    mock.patch.object(agent.subprocess, 'run') as run_mock:
                result = agent._read_info_plist(path)
            self.assertEqual(result['CFBundleVersion'], '99')
            self.assertEqual(run_mock.call_count, 0)
        finally:
            os.unlink(path)

    def test_read_plist_with_none_path_returns_empty(self):
        # The early-return guard for None path runs before
        # resolveCli so the CLI is never invoked.
        with mock.patch.object(agent, 'resolveCli') as cli_mock:
            result = agent._read_info_plist(None)
        self.assertEqual(result, {})
        self.assertEqual(cli_mock.call_count, 0)


if __name__ == '__main__':
    unittest.main()

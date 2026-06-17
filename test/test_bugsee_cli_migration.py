#!/usr/bin/env python3
"""Tests for the bugsee-cli migration in BugseeAgent.

Loads BugseeAgent as a module (it has no .py extension) and exercises
the CLI resolver, version-fallback chain, dSYM walk, and main() flow
with all external commands (dwarfdump, dsymutil, tar, bugsee-cli) and
all HTTP calls (urllib) mocked.

Each test asserts something specific about behaviour — argv shape that
the bugsee-cli binary will see, the host-triple → URL mapping that
download requests will use, what happens on a SHA-256 mismatch, etc. —
NOT just "the function returned non-None". A future regression in the
production path is meant to make one of these tests fail loudly, not
slip past a coverage report.

Run from the repo root:
    python3 -m unittest discover -s test -v

Or directly:
    python3 -m unittest test.test_bugsee_cli_migration -v
"""

import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import ANY, MagicMock, patch


# ──────────────────────────────────────────────────────────────────
# Load BugseeAgent as a module. It has no .py extension and lives at
# the repo root, so importlib.util is the cleanest path. Module-level
# code in BugseeAgent only defines functions/constants — the
# `if __name__ == "__main__"` block (option parsing, daemonize) does
# NOT run on import, which is what we want.
# ──────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_BUGSEE_AGENT_PATH = os.path.normpath(os.path.join(_HERE, "..", "BugseeAgent"))
# BugseeAgent has no .py extension so spec_from_file_location can't pick
# a loader by suffix. Construct a SourceFileLoader explicitly instead.
import importlib.machinery
_loader = importlib.machinery.SourceFileLoader(
    "bugsee_agent_under_test", _BUGSEE_AGENT_PATH,
)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
agent = importlib.util.module_from_spec(_spec)
_loader.exec_module(agent)


# ──────────────────────────────────────────────────────────────────
# Host triple detection — the auto-download URL is built from this,
# so a mismatch here means the plugin downloads a 404.
# ──────────────────────────────────────────────────────────────────
class TestDetectHostTriple(unittest.TestCase):
    def _mock(self, system, machine):
        return patch.multiple(
            agent.platform,
            system=MagicMock(return_value=system),
            machine=MagicMock(return_value=machine),
        )

    def test_macos_apple_silicon_arm64(self):
        with self._mock("Darwin", "arm64"):
            self.assertEqual(agent.detectHostTriple(), "aarch64-apple-darwin")

    def test_macos_apple_silicon_aarch64_alias(self):
        # Some Pythons report machine as `aarch64` even on Apple Silicon.
        with self._mock("Darwin", "aarch64"):
            self.assertEqual(agent.detectHostTriple(), "aarch64-apple-darwin")

    def test_macos_intel_x86_64(self):
        with self._mock("Darwin", "x86_64"):
            self.assertEqual(agent.detectHostTriple(), "x86_64-apple-darwin")

    def test_macos_intel_amd64_alias(self):
        with self._mock("Darwin", "amd64"):
            self.assertEqual(agent.detectHostTriple(), "x86_64-apple-darwin")

    def test_linux_arm64(self):
        with self._mock("Linux", "aarch64"):
            self.assertEqual(agent.detectHostTriple(), "aarch64-unknown-linux-gnu")

    def test_linux_amd64(self):
        with self._mock("Linux", "x86_64"):
            self.assertEqual(agent.detectHostTriple(), "x86_64-unknown-linux-gnu")

    def test_windows_amd64(self):
        with self._mock("Windows", "AMD64"):
            self.assertEqual(agent.detectHostTriple(), "x86_64-pc-windows-msvc")

    def test_unsupported_freebsd_returns_none(self):
        # dist does not publish a FreeBSD target; returning None lets
        # the caller fall back / skip rather than 404.
        with self._mock("FreeBSD", "x86_64"):
            self.assertIsNone(agent.detectHostTriple())

    def test_unsupported_windows_arm64_returns_none(self):
        # No Windows ARM64 binary as of v0.1.x.
        with self._mock("Windows", "ARM64"):
            self.assertIsNone(agent.detectHostTriple())

    def test_unsupported_linux_i386_returns_none(self):
        with self._mock("Linux", "i386"):
            self.assertIsNone(agent.detectHostTriple())


# ──────────────────────────────────────────────────────────────────
# resolveCli — orchestrates explicit-path / cache-hit / auto-download
# layers. Tests use a controlled $HOME so production caches are
# untouched.
# ──────────────────────────────────────────────────────────────────
class TestResolveCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(self.fake_home, exist_ok=True)
        self._old_env = {
            "HOME": os.environ.get("HOME"),
            "BUGSEE_CLI_PATH": os.environ.get("BUGSEE_CLI_PATH"),
            "BUGSEE_CLI_VERSION": os.environ.get("BUGSEE_CLI_VERSION"),
            "BUGSEE_CLI_AUTO_UPDATE": os.environ.get("BUGSEE_CLI_AUTO_UPDATE"),
        }
        os.environ["HOME"] = self.fake_home
        os.environ.pop("BUGSEE_CLI_PATH", None)
        os.environ.pop("BUGSEE_CLI_VERSION", None)
        # Disable the auto-update network pointer check for the existing
        # default-version resolution tests so they deterministically use
        # BUGSEE_CLI_DEFAULT_VERSION (the floor) as the cache dir without
        # reaching out to download.bugsee.com. The auto-update path has
        # its own dedicated coverage (TestAutoUpdateContract /
        # TestResolveEffectiveVersion below).
        os.environ["BUGSEE_CLI_AUTO_UPDATE"] = "0"
        # `resolveCli` caches its results for the lifetime of the
        # process. Each test mutates env + fake home, so wipe the
        # cache to keep tests independent of execution order.
        agent._resolveCli_cache.clear()
        # Reset the once-per-process self-update guard so a managed-binary
        # resolution in one test doesn't suppress it in the next.
        agent._self_update_done = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _make_executable(self, path, content="#!/bin/sh\nexit 0\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        os.chmod(path, 0o755)

    def test_explicit_path_executable_returns_verbatim(self):
        bin_path = os.path.join(self.tmp, "my-cli")
        self._make_executable(bin_path)
        self.assertEqual(agent.resolveCli(cliPath=bin_path), bin_path)

    def test_explicit_env_var_path_executable_returns_verbatim(self):
        bin_path = os.path.join(self.tmp, "env-cli")
        self._make_executable(bin_path)
        os.environ["BUGSEE_CLI_PATH"] = bin_path
        self.assertEqual(agent.resolveCli(), bin_path)

    def test_explicit_path_missing_falls_through_to_download(self):
        # When the explicit path is unusable AND auto-download fails
        # (unsupported triple), we expect None — not an exception.
        with patch.object(agent, "detectHostTriple", return_value=None):
            self.assertIsNone(agent.resolveCli(cliPath="/no/such/file"))

    def test_unsupported_host_returns_none_without_download(self):
        with patch.object(agent, "detectHostTriple", return_value=None), \
             patch.object(agent, "_downloadCli") as mock_dl:
            self.assertIsNone(agent.resolveCli())
            mock_dl.assert_not_called()

    def test_cache_hit_returns_immediately_without_download(self):
        triple = "aarch64-apple-darwin"
        # Track the agent's actual default version rather than a hard-coded
        # literal, so repinning BUGSEE_CLI_DEFAULT_VERSION can't break this.
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli",
            agent.BUGSEE_CLI_DEFAULT_VERSION, triple, "bugsee-cli",
        )
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli") as mock_dl:
            self.assertEqual(agent.resolveCli(), cache_bin)
            mock_dl.assert_not_called()

    def test_uses_custom_cli_version(self):
        # Custom version → cache dir contains custom version, not the default.
        triple = "aarch64-apple-darwin"
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli", "9.9.9", triple, "bugsee-cli",
        )
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple):
            self.assertEqual(agent.resolveCli(cliVersion="9.9.9"), cache_bin)

    def test_env_cli_version_overrides_default(self):
        os.environ["BUGSEE_CLI_VERSION"] = "0.2.3"
        triple = "x86_64-apple-darwin"
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli", "0.2.3", triple, "bugsee-cli",
        )
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple):
            self.assertEqual(agent.resolveCli(), cache_bin)

    def test_resolves_cli_only_once_for_repeat_calls(self):
        # Memoization pin. The fastlane plugin invokes resolveCli()
        # from EVERY CLI helper (8+ times per build). Without
        # memoization, each call walked the filesystem looking for a
        # cached binary AND re-ran detectHostTriple. The cache should
        # eliminate every call after the first under identical
        # env+args.
        bin_path = os.path.join(self.tmp, "cached-cli")
        self._make_executable(bin_path)
        os.environ["BUGSEE_CLI_PATH"] = bin_path
        with patch.object(agent, "_resolveCli_uncached",
                          return_value=bin_path) as inner_mock:
            self.assertEqual(agent.resolveCli(), bin_path)
            self.assertEqual(agent.resolveCli(), bin_path)
            self.assertEqual(agent.resolveCli(), bin_path)
        self.assertEqual(
            inner_mock.call_count, 1,
            "resolveCli must only invoke the uncached resolver once "
            "for repeat calls with identical env + args",
        )

    def test_cache_keys_on_env_var_change(self):
        # Defensive pin: if BUGSEE_CLI_PATH flips between resolveCli
        # invocations (a malicious CI step swapping the binary,
        # genuinely), the cache must not silently return the
        # previously-resolved binary — the env-var changes are part
        # of the cache key. (In practice the env is constant per
        # build, but the cache must NOT trap state-poisoning.)
        bin_a = os.path.join(self.tmp, "cli-a")
        bin_b = os.path.join(self.tmp, "cli-b")
        self._make_executable(bin_a)
        self._make_executable(bin_b)
        os.environ["BUGSEE_CLI_PATH"] = bin_a
        self.assertEqual(agent.resolveCli(), bin_a)
        os.environ["BUGSEE_CLI_PATH"] = bin_b
        # Cache key includes the env var → flip should re-resolve.
        self.assertEqual(agent.resolveCli(), bin_b)

    def test_rejects_malformed_cli_version_traversal_attempt(self):
        # Path-traversal pin. Pre-fix, a malicious BUGSEE_CLI_VERSION
        # like `../../evil` would land the cached binary at
        # `~/.bugsee/cli/../../evil/<triple>/bugsee-cli` — outside the
        # intended cache root — AND get baked into the download URL.
        # The strict X.Y.Z[-prerelease] regex now rejects it before
        # either path uses the value, returning None as a soft
        # failure.
        os.environ["BUGSEE_CLI_VERSION"] = "../../evil"
        triple = "aarch64-apple-darwin"
        with patch.object(agent, "detectHostTriple", return_value=triple):
            with patch.object(agent, "_downloadCli") as dl_mock:
                self.assertIsNone(agent.resolveCli())
                # Download must NOT be invoked — the rejection happens
                # before any network / disk activity.
                dl_mock.assert_not_called()

    def test_rejects_malformed_cli_version_with_slash(self):
        # Mirror pin for URL-injection-style abuse. A version like
        # `1.0/../evil` would otherwise both create unintended cache
        # subdirs AND inject path segments into the download URL.
        with patch.object(agent, "detectHostTriple", return_value="aarch64-apple-darwin"), \
             patch.object(agent, "_downloadCli") as dl_mock:
            self.assertIsNone(agent.resolveCli(cliVersion="1.0/../evil"))
            dl_mock.assert_not_called()

    def test_accepts_canonical_semver_prerelease(self):
        # Positive pin: well-formed prerelease versions DO pass the
        # validator (e.g. "1.2.3-rc.1", "0.1.1-beta+sha.abc"). Ensures
        # the regex isn't so strict it breaks legitimate releases.
        triple = "aarch64-apple-darwin"
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli", "1.2.3-rc.1", triple, "bugsee-cli",
        )
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple):
            self.assertEqual(
                agent.resolveCli(cliVersion="1.2.3-rc.1"),
                cache_bin,
            )

    def test_download_exception_returns_none_not_raise(self):
        # A network failure (or 404, SHA mismatch, etc.) inside
        # _downloadCli must surface as a soft failure — main() needs
        # the option to skip without crashing the build.
        with patch.object(agent, "detectHostTriple", return_value="aarch64-apple-darwin"), \
             patch.object(agent, "_downloadCli", side_effect=IOError("network down")):
            self.assertIsNone(agent.resolveCli())

    def test_download_succeeds_then_binary_returned(self):
        triple = "aarch64-apple-darwin"
        # Track the agent's actual default version (see cache-hit test).
        default_version = agent.BUGSEE_CLI_DEFAULT_VERSION
        cache_dir = os.path.join(self.fake_home, ".bugsee", "cli", default_version, triple)
        cache_bin = os.path.join(cache_dir, "bugsee-cli")

        def fake_download(version, target_triple, target_dir):
            # Simulate what _downloadCli does on success.
            self.assertEqual(version, default_version)
            self.assertEqual(target_triple, triple)
            self.assertEqual(target_dir, cache_dir)
            self._make_executable(cache_bin)

        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli", side_effect=fake_download):
            self.assertEqual(agent.resolveCli(), cache_bin)


# ──────────────────────────────────────────────────────────────────
# _downloadCli — verifies the URL constructed, that the SHA-256
# sidecar gates the extraction, and that the wrong checksum is fatal.
# ──────────────────────────────────────────────────────────────────
class TestDownloadCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_urlopen(self, payloads):
        """Return a factory yielding a file-like response for each URL in
        `payloads` (dict mapping URL → bytes). Uses io.BytesIO so
        subsequent `read()` calls return empty bytes (EOF) naturally —
        critical because `shutil.copyfileobj` loops on read(), and a
        MagicMock with `return_value=payload` would yield the same
        non-empty bytes forever (CPU spin + unbounded mock_calls list,
        observed as a tens-of-GB memory blow-up).

        An unknown URL raises so an unexpected fetch fails loudly
        instead of silently returning empty bytes."""
        def factory(url, *args, **kwargs):
            self.assertIn(url, payloads, f"unexpected GET to {url}")
            return io.BytesIO(payloads[url])
        return factory

    def test_unix_url_uses_tar_xz(self):
        # On Unix triples, the artifact extension is .tar.xz.
        # The SHA sidecar comes from the same URL plus `.sha256`.
        tarball_bytes = b"fake tarball contents"
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        urls = {
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-aarch64-apple-darwin.tar.xz":
                tarball_bytes,
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-aarch64-apple-darwin.tar.xz.sha256":
                f"{expected_sha}  bugsee-cli-aarch64-apple-darwin.tar.xz\n".encode(),
        }
        with patch.object(agent.urllib.request, "urlopen", side_effect=self._stub_urlopen(urls)), \
             patch.object(agent.subprocess, "run") as mock_run:
            # Successful tar extraction; produce the expected binary on disk
            def fake_tar(cmd, *a, **k):
                # cmd is ["tar", "-xf", tarball_path, "-C", cacheDir, "--strip-components=1"]
                self.assertEqual(cmd[0], "tar")
                self.assertEqual(cmd[1], "-xf")
                self.assertEqual(cmd[3], "-C")
                self.assertEqual(cmd[4], self.tmp)
                self.assertEqual(cmd[5], "--strip-components=1")
                # Write the binary that downloadCli expects to find at the end.
                with open(os.path.join(self.tmp, "bugsee-cli"), "w") as f:
                    f.write("binary")
                return MagicMock(returncode=0, stderr="")
            mock_run.side_effect = fake_tar
            agent._downloadCli("0.1.1", "aarch64-apple-darwin", self.tmp)
            # Binary should now be present + executable.
            bin_path = os.path.join(self.tmp, "bugsee-cli")
            self.assertTrue(os.path.isfile(bin_path))
            self.assertTrue(os.access(bin_path, os.X_OK))

    def test_windows_url_uses_zip(self):
        # On Windows triples the artifact extension flips to .zip.
        tarball_bytes = b"fake zip contents"
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        urls = {
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-pc-windows-msvc.zip":
                tarball_bytes,
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-pc-windows-msvc.zip.sha256":
                f"{expected_sha}  bugsee-cli-x86_64-pc-windows-msvc.zip\n".encode(),
        }
        with patch.object(agent.urllib.request, "urlopen", side_effect=self._stub_urlopen(urls)), \
             patch.object(agent.subprocess, "run") as mock_run:
            def fake_tar(cmd, *a, **k):
                # On Windows the binary inside is bugsee-cli.exe
                with open(os.path.join(self.tmp, "bugsee-cli.exe"), "w") as f:
                    f.write("binary")
                return MagicMock(returncode=0, stderr="")
            mock_run.side_effect = fake_tar
            agent._downloadCli("0.1.1", "x86_64-pc-windows-msvc", self.tmp)
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, "bugsee-cli.exe")))

    def test_sha_mismatch_raises_and_removes_tarball(self):
        # An attacker (or a corrupted CDN cache) shipping different
        # bytes than the published checksum must result in a hard
        # error — not silent installation of unverified content.
        tarball_bytes = b"these bytes will not match the sidecar's SHA"
        bogus_sha = "0" * 64
        urls = {
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-aarch64-apple-darwin.tar.xz":
                tarball_bytes,
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-aarch64-apple-darwin.tar.xz.sha256":
                f"{bogus_sha}  bugsee-cli-aarch64-apple-darwin.tar.xz\n".encode(),
        }
        with patch.object(agent.urllib.request, "urlopen", side_effect=self._stub_urlopen(urls)), \
             patch.object(agent.subprocess, "run") as mock_run:
            with self.assertRaises(IOError) as ctx:
                agent._downloadCli("0.1.1", "aarch64-apple-darwin", self.tmp)
            self.assertIn("SHA-256 mismatch", str(ctx.exception))
            # tar should NEVER have been invoked because verify happens first
            mock_run.assert_not_called()
            # The bad tarball should have been deleted, not left behind
            self.assertFalse(os.path.exists(
                os.path.join(self.tmp, "bugsee-cli-aarch64-apple-darwin.tar.xz")
            ))

    def test_sha_sidecar_with_asterisk_binary_mode_marker_parses(self):
        # sha256sum's "binary mode" format is `<hex> *<filename>`.
        # Must still extract the hex correctly.
        tarball_bytes = b"fake tarball"
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        urls = {
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-unknown-linux-gnu.tar.xz":
                tarball_bytes,
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-unknown-linux-gnu.tar.xz.sha256":
                f"{expected_sha} *bugsee-cli-x86_64-unknown-linux-gnu.tar.xz\n".encode(),
        }
        with patch.object(agent.urllib.request, "urlopen", side_effect=self._stub_urlopen(urls)), \
             patch.object(agent.subprocess, "run") as mock_run:
            def fake_tar(cmd, *a, **k):
                with open(os.path.join(self.tmp, "bugsee-cli"), "w") as f:
                    f.write("binary")
                return MagicMock(returncode=0, stderr="")
            mock_run.side_effect = fake_tar
            # Should NOT raise SHA mismatch — the asterisk is just a mode marker.
            agent._downloadCli("0.1.1", "x86_64-unknown-linux-gnu", self.tmp)

    def test_tar_failure_raises_ioerror_with_stderr(self):
        # tar non-zero exit should surface a useful error message.
        tarball_bytes = b"fake tarball"
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        urls = {
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-apple-darwin.tar.xz":
                tarball_bytes,
            "https://download.bugsee.com/cli/v0.1.1/bugsee-cli-x86_64-apple-darwin.tar.xz.sha256":
                f"{expected_sha}\n".encode(),
        }
        with patch.object(agent.urllib.request, "urlopen", side_effect=self._stub_urlopen(urls)), \
             patch.object(agent.subprocess, "run", return_value=MagicMock(
                returncode=2, stderr="tar: bad header\n")):
            with self.assertRaises(IOError) as ctx:
                agent._downloadCli("0.1.1", "x86_64-apple-darwin", self.tmp)
            self.assertIn("tar -xf", str(ctx.exception))
            self.assertIn("bad header", str(ctx.exception))


# ──────────────────────────────────────────────────────────────────
# uploadDsymViaCli — the argv shape the bugsee-cli binary receives is
# a wire contract: a future rename of any CLI flag must surface here.
# ──────────────────────────────────────────────────────────────────
class TestUploadDsymViaCli(unittest.TestCase):
    def test_argv_shape_pinned(self):
        with patch.object(agent.subprocess, "run", return_value=MagicMock(returncode=0)) as mock_run:
            ok = agent.uploadDsymViaCli(
                "/path/to/bugsee-cli",
                "/path/to/Foo.dSYM",
                "the-app-token",
                "https://apidev.bugsee.com",
                "1.2.3",
                "42",
            )
            self.assertTrue(ok)
            mock_run.assert_called_once()
            argv = mock_run.call_args[0][0]
            self.assertEqual(argv, [
                "/path/to/bugsee-cli",
                "--endpoint", "https://apidev.bugsee.com",
                "--app-token", "the-app-token",
                "debug-files", "upload",
                "--type", "dsym",
                "--version", "1.2.3",
                "--build", "42",
                "/path/to/Foo.dSYM",
            ])

    def test_returns_false_on_nonzero_exit(self):
        with patch.object(agent.subprocess, "run", return_value=MagicMock(returncode=30)):
            self.assertFalse(agent.uploadDsymViaCli(
                "/x", "/y", "t", "e", "v", "b"))

    def test_returns_false_on_subprocess_exception(self):
        # OSError can happen if the binary is unexpectedly removed
        # mid-run, or if the kernel refuses the exec (wrong arch).
        with patch.object(agent.subprocess, "run", side_effect=OSError("ENOEXEC")):
            self.assertFalse(agent.uploadDsymViaCli(
                "/x", "/y", "t", "e", "v", "b"))

    def test_none_version_and_build_pass_empty_string(self):
        # The CLI requires non-empty values; we substitute "" to make
        # the failure observable downstream rather than passing literal
        # "None" strings.
        with patch.object(agent.subprocess, "run", return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadDsymViaCli("/cli", "/dsym", "t", "e", None, None)
            argv = mock_run.call_args[0][0]
            self.assertIn("--version", argv)
            self.assertIn("--build", argv)
            v_idx = argv.index("--version")
            b_idx = argv.index("--build")
            self.assertEqual(argv[v_idx + 1], "")
            self.assertEqual(argv[b_idx + 1], "")


# ──────────────────────────────────────────────────────────────────
# uploadMappingViaCli — Android ProGuard / R8 mapping.txt upload
# via `bugsee-cli debug-files upload --type proguard`. Argv shape
# matches what the Android Gradle plugin's MappingUploadTask
# produces — the fastlane Ruby action resolves the UUID upstream and
# we just pass it through.
# ──────────────────────────────────────────────────────────────────
class TestUploadMappingViaCli(unittest.TestCase):
    def test_argv_shape_pinned_no_icon(self):
        # All flags in deterministic order so a regression in the
        # CLI's option ordering or our argv builder fails this test
        # clearly. The order matches CliUploader.kt's
        # buildMappingArgv — that's the cross-repo wire contract.
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            ok = agent.uploadMappingViaCli(
                "/path/to/bugsee-cli", "/path/to/mapping.txt",
                "app-tok", "https://api.bugsee.com",
                "1.2.3", "42", "the-uuid",
            )
            self.assertTrue(ok)
            argv = mock_run.call_args[0][0]
            self.assertEqual(argv, [
                "/path/to/bugsee-cli",
                "--endpoint", "https://api.bugsee.com",
                "--app-token", "app-tok",
                "debug-files", "upload",
                "--type", "proguard",
                "--version", "1.2.3",
                "--build", "42",
                "--uuid", "the-uuid",
                "/path/to/mapping.txt",
            ])

    def test_argv_includes_icon_flag_when_provided(self):
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadMappingViaCli(
                "/cli", "/mapping.txt", "tok", "ep",
                "1.0", "1", "uuid-x", "/icon.png",
            )
            argv = mock_run.call_args[0][0]
            self.assertIn("--icon", argv)
            icon_idx = argv.index("--icon")
            self.assertEqual(argv[icon_idx + 1], "/icon.png")
            # Mapping path remains the LAST positional after --icon
            # so the CLI's argparse treats it as the path arg, not
            # the icon arg.
            self.assertEqual(argv[-1], "/mapping.txt")

    def test_argv_omits_icon_flag_when_none(self):
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadMappingViaCli(
                "/cli", "/mapping.txt", "tok", "ep",
                "1.0", "1", "uuid-x", None,
            )
            argv = mock_run.call_args[0][0]
            self.assertNotIn("--icon", argv)

    def test_argv_omits_icon_flag_when_empty_string(self):
        # Defensive: passing an empty string should also drop the
        # flag (the Ruby action coerces missing-but-given to nil
        # before invoking BugseeAgent; pin the agent-side defense too).
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadMappingViaCli(
                "/cli", "/mapping.txt", "tok", "ep",
                "1.0", "1", "uuid-x", "",
            )
            argv = mock_run.call_args[0][0]
            self.assertNotIn("--icon", argv)

    def test_returns_false_on_nonzero_exit(self):
        # CLI exit non-zero must surface as False so the Ruby
        # action's caller can react (the action itself swallows the
        # failure via UI.error to keep the lane running).
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=1)):
            self.assertFalse(agent.uploadMappingViaCli(
                "/cli", "/mapping", "t", "e", "v", "b", "u",
            ))

    def test_returns_false_on_subprocess_exception(self):
        # ENOEXEC / FileNotFoundError on a broken or missing CLI
        # binary must surface as False (and log) rather than
        # bubbling up.
        with patch.object(agent.subprocess, "run",
                          side_effect=OSError("ENOEXEC")):
            self.assertFalse(agent.uploadMappingViaCli(
                "/cli", "/mapping", "t", "e", "v", "b", "u",
            ))

    def test_none_version_and_build_pass_empty_string(self):
        # Same defensive behaviour as the dSYM upload variant: empty
        # string downstream rather than literal "None".
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadMappingViaCli(
                "/cli", "/mapping", "t", "e", None, None, "u",
            )
            argv = mock_run.call_args[0][0]
            v_idx = argv.index("--version")
            b_idx = argv.index("--build")
            self.assertEqual(argv[v_idx + 1], "")
            self.assertEqual(argv[b_idx + 1], "")

    def test_none_uuid_passes_empty_string(self):
        # The Ruby action ALWAYS resolves UUID upstream (explicit /
        # file / synthesis), but if the agent ever runs with a None
        # UUID for any reason, pass "" so the CLI's UUID parse
        # surfaces an explicit error rather than crashing inside
        # urllib's str-encoding of None.
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=0)) as mock_run:
            agent.uploadMappingViaCli(
                "/cli", "/mapping", "t", "e", "v", "b", None,
            )
            argv = mock_run.call_args[0][0]
            u_idx = argv.index("--uuid")
            self.assertEqual(argv[u_idx + 1], "")


# ──────────────────────────────────────────────────────────────────
# parseDSYM — UUID extraction from dwarfdump output. Real dwarfdump
# is mocked so tests are macOS/Linux portable.
# ──────────────────────────────────────────────────────────────────
class TestParseDSYM(unittest.TestCase):
    def _stub_dwarfdump(self, stdout):
        return patch.object(
            agent.subprocess, "run",
            return_value=MagicMock(stdout=stdout, returncode=0),
        )

    def test_single_arch_dsym(self):
        out = "UUID: 54D75FB3-747F-387F-8A93-4EA034B1F8CF (x86_64) /path/binary\n"
        with self._stub_dwarfdump(out):
            self.assertEqual(
                agent.parseDSYM("/path/binary"),
                ["54D75FB3-747F-387F-8A93-4EA034B1F8CF"],
            )

    def test_multi_arch_fat_dsym(self):
        out = (
            "UUID: 54D75FB3-747F-387F-8A93-4EA034B1F8CF (x86_64) /path/binary\n"
            "UUID: 831BB3B1-C969-3638-B7F5-BF43B3CF8AB3 (arm64) /path/binary\n"
        )
        with self._stub_dwarfdump(out):
            self.assertEqual(
                agent.parseDSYM("/path/binary"),
                [
                    "54D75FB3-747F-387F-8A93-4EA034B1F8CF",
                    "831BB3B1-C969-3638-B7F5-BF43B3CF8AB3",
                ],
            )

    def test_dwarfdump_nonzero_exit_returns_empty(self):
        # Corrupt or non-Mach-O input must surface as empty list, not crash.
        with patch.object(
            agent.subprocess, "run",
            side_effect=subprocess.CalledProcessError(1, "dwarfdump"),
        ):
            self.assertEqual(agent.parseDSYM("/bad/file"), [])

    def test_no_uuid_lines_in_output(self):
        # dwarfdump might emit only warnings/headers; no UUID line.
        with self._stub_dwarfdump("warning: file is not a debug-info-bearing Mach-O\n"):
            self.assertEqual(agent.parseDSYM("/path"), [])

    # ── Option-C migration: `bugsee-cli dsym uuid` ──────────────────

    def test_uses_cli_subcommand_when_cli_is_available(self):
        # Production parseDSYM tries `bugsee-cli dsym uuid` first.
        # When the CLI returns a valid JSON array, that wins and
        # dwarfdump is never invoked.
        canned = '["54D75FB3-747F-387F-8A93-4EA034B1F8CF",' \
                 '"831BB3B1-C969-3638-B7F5-BF43B3CF8AB3"]'
        with patch.object(agent, 'resolveCli',
                          return_value='/usr/local/bin/bugsee-cli'), \
             patch.object(agent.subprocess, 'run',
                          return_value=MagicMock(returncode=0, stdout=canned)) as run_mock:
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, [
            "54D75FB3-747F-387F-8A93-4EA034B1F8CF",
            "831BB3B1-C969-3638-B7F5-BF43B3CF8AB3",
        ])
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[0], '/usr/local/bin/bugsee-cli')
        self.assertIn('dsym', argv)
        self.assertIn('uuid', argv)
        # The binary path is the last positional argument.
        self.assertEqual(argv[-1], '/some/binary')

    def test_falls_back_to_dwarfdump_when_cli_unavailable(self):
        # resolveCli returns None → dwarfdump path runs. Verify
        # the dwarfdump branch produces the expected UUID.
        with patch.object(agent, 'resolveCli', return_value=None), \
             self._stub_dwarfdump(
                 "UUID: 11111111-1111-1111-1111-111111111111 (arm64) /x\n"):
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, ["11111111-1111-1111-1111-111111111111"])

    def test_falls_back_to_dwarfdump_on_cli_nonzero_exit(self):
        # CLI exits 2 (older bugsee-cli without the subcommand)
        # → dwarfdump fallback runs. The two subprocess.run calls
        # are routed via a side_effect chain.
        responses = [
            MagicMock(returncode=2, stdout=''),  # CLI fails
            MagicMock(returncode=0,
                      stdout="UUID: 22222222-2222-2222-2222-222222222222 (arm64) /x\n"),
        ]
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          side_effect=responses):
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, ["22222222-2222-2222-2222-222222222222"])

    def test_falls_back_to_dwarfdump_on_malformed_cli_json(self):
        # CLI returns garbage → ValueError → dwarfdump fallback.
        responses = [
            MagicMock(returncode=0, stdout='not json'),
            MagicMock(returncode=0,
                      stdout="UUID: 33333333-3333-3333-3333-333333333333 (arm64) /x\n"),
        ]
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          side_effect=responses):
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, ["33333333-3333-3333-3333-333333333333"])

    def test_falls_back_to_dwarfdump_on_oserror(self):
        # CLI exec raises OSError (ENOEXEC, FileNotFoundError)
        # → fallback to dwarfdump.
        responses = [
            OSError("ENOEXEC"),
            MagicMock(returncode=0,
                      stdout="UUID: 44444444-4444-4444-4444-444444444444 (arm64) /x\n"),
        ]
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          side_effect=responses):
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, ["44444444-4444-4444-4444-444444444444"])

    def test_cli_path_returns_empty_list_on_no_uuids(self):
        # When the CLI succeeds with an empty array, parseDSYM
        # returns [] and does NOT fall back to dwarfdump (the
        # CLI is authoritative for "this file has no Mach-O
        # UUIDs"). Same posture as the SDK's plist value
        # subcommand.
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          return_value=MagicMock(returncode=0, stdout='[]')) as run_mock:
            result = agent.parseDSYM('/some/binary')
        self.assertEqual(result, [])
        # Exactly one subprocess.run call — the CLI. No dwarfdump
        # fallback fired.
        self.assertEqual(run_mock.call_count, 1)


# ──────────────────────────────────────────────────────────────────
# Cache file helpers — the "already uploaded UUIDs" short-circuit
# saves a CLI exec + HTTP round-trip per duplicate.
# ──────────────────────────────────────────────────────────────────
class TestUploadedListCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home

    def test_load_when_file_missing_returns_empty(self):
        self.assertEqual(agent.loadUploadedList(), [])

    def test_save_then_load_roundtrip_preserves_uuids(self):
        uuids = [
            "54D75FB3-747F-387F-8A93-4EA034B1F8CF",
            "831BB3B1-C969-3638-B7F5-BF43B3CF8AB3",
        ]
        agent.saveUploadedList(uuids)
        self.assertEqual(agent.loadUploadedList(), uuids)

    def test_in_uploaded_list_partial_overlap(self):
        existing = ["A", "B", "C"]
        # A single overlapping UUID is enough to mark the dSYM as seen
        # (because we'd be uploading bytes the server already has).
        self.assertTrue(agent.isInUploadedList(["B"], existing))
        self.assertTrue(agent.isInUploadedList(["X", "B"], existing))

    def test_in_uploaded_list_no_overlap(self):
        self.assertFalse(agent.isInUploadedList(["X", "Y"], ["A", "B"]))

    def test_in_uploaded_list_empty_inputs(self):
        self.assertFalse(agent.isInUploadedList([], []))
        self.assertFalse(agent.isInUploadedList([], ["A"]))
        self.assertFalse(agent.isInUploadedList(["A"], []))


# ──────────────────────────────────────────────────────────────────
# Integration: full main() flow with all subprocess + HTTP calls
# mocked. Verifies the bugsee-cli binary IS exec'd per dSYM with the
# correct argv, that the cache is updated only on success, and that
# the version/build fallback chain works.
# ──────────────────────────────────────────────────────────────────
class TestMainIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(self.fake_home)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.fake_home
        # Drop the cache file if it exists in the fake home.
        cache = os.path.join(self.fake_home, ".bugseeUploadList")
        if os.path.exists(cache):
            os.unlink(cache)

        # Build two synthetic .dSYM bundles under self.tmp.
        self.dsym_a = os.path.join(self.tmp, "Foo.dSYM")
        self.dwarf_a = os.path.join(self.dsym_a, "Contents", "Resources", "DWARF")
        os.makedirs(self.dwarf_a)
        with open(os.path.join(self.dwarf_a, "Foo"), "w") as f:
            f.write("synthetic Mach-O bytes")

        self.dsym_b = os.path.join(self.tmp, "Bar.dSYM")
        self.dwarf_b = os.path.join(self.dsym_b, "Contents", "Resources", "DWARF")
        os.makedirs(self.dwarf_b)
        with open(os.path.join(self.dwarf_b, "Bar"), "w") as f:
            f.write("synthetic Mach-O bytes")

        # Fake bugsee-cli binary.
        self.cli_path = os.path.join(self.tmp, "bugsee-cli")
        with open(self.cli_path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.cli_path, 0o755)

        # Synthesize the `options` namespace main() reads from. The
        # real entry point's option-parsing block is not executed when
        # we import BugseeAgent as a module, so we fabricate a stand-in.
        self.options = type("Opts", (), {
            "version": None,
            "build": None,
            "dsym_list": False,
            "dsym_folder": self.tmp,
            "symbol_maps": None,
            "endpoint": "https://apidev.bugsee.com",
            "cli_path": self.cli_path,
            "cli_version": None,
            "from_xcode": False,
            "build_dir": None,
            "collect_deps": False,
        })()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _setup_module_state(self, version, build):
        """Install `options`/`args`/`APP_TOKEN` at module level so
        main() can read them the same way it does at runtime."""
        self.options.version = version
        self.options.build = build
        agent.options = self.options
        agent.args = ["the-token"]
        agent.APP_TOKEN = "the-token"

    def test_uploads_each_dsym_via_cli_with_correct_argv(self):
        self._setup_module_state(version="1.2.3", build="42")
        # Synthetic dwarfdump output for each binary inside DWARF/.
        # Different UUIDs per dSYM so the cache distinguishes them.
        dwarf_outputs = {
            os.path.join(self.dwarf_a, "Foo"):
                "UUID: AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA (arm64) /x/Foo\n",
            os.path.join(self.dwarf_b, "Bar"):
                "UUID: BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB (arm64) /x/Bar\n",
        }

        cli_calls = []

        # The `bugsee-cli dsym uuid <macho>` subcommand (Option-C
        # migration) is what parseDSYM now prefers. parseDSYM is
        # called with the SAME Mach-O leaf path that dwarfdump
        # would have received, so we key off `dwarf_outputs` and
        # extract the UUID from its canned text output. cli_calls
        # only counts the debug-files upload invocations.
        def _dsym_uuid_json_for(macho_path):
            text = dwarf_outputs.get(macho_path, "")
            import json as _json
            m = re.search(r'UUID:\s+([0-9A-Fa-f-]+)', text)
            return _json.dumps([m.group(1)] if m else [])

        def fake_subprocess_run(cmd, *a, **k):
            if cmd[0] == "/usr/bin/dwarfdump":
                # dwarfdump returns a fake UUID per file.
                return MagicMock(stdout=dwarf_outputs.get(cmd[2], ""), returncode=0)
            if cmd[0] == self.cli_path:
                if len(cmd) >= 3 and cmd[1] == "dsym" and cmd[2] == "uuid":
                    return MagicMock(
                        stdout=_dsym_uuid_json_for(cmd[3]),
                        returncode=0,
                    )
                cli_calls.append(list(cmd))
                return MagicMock(returncode=0)
            raise AssertionError("unexpected subprocess: %r" % cmd)

        with patch.object(agent.subprocess, "run", side_effect=fake_subprocess_run):
            agent.main()

        self.assertEqual(len(cli_calls), 2,
            "exactly one CLI invocation per dSYM expected; got %r" % cli_calls)
        # Both invocations point at distinct .dSYM bundle directories.
        dsym_paths = sorted(call[-1] for call in cli_calls)
        self.assertEqual(dsym_paths, sorted([self.dsym_a, self.dsym_b]))
        # Every invocation carries the version/build supplied via options.
        for cmd in cli_calls:
            self.assertEqual(cmd[cmd.index("--version") + 1], "1.2.3")
            self.assertEqual(cmd[cmd.index("--build") + 1], "42")
            self.assertEqual(cmd[cmd.index("--app-token") + 1], "the-token")
            self.assertEqual(cmd[cmd.index("--type") + 1], "dsym")

    def test_cache_hit_skips_cli_exec(self):
        self._setup_module_state(version="1.0", build="1")
        # Pre-populate the cache with both UUIDs so both dSYMs are
        # already considered "uploaded".
        seeded_uuids = ["AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
                        "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"]
        agent.saveUploadedList(seeded_uuids)
        dwarf_outputs = {
            os.path.join(self.dwarf_a, "Foo"):
                "UUID: AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA (arm64) /Foo\n",
            os.path.join(self.dwarf_b, "Bar"):
                "UUID: BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB (arm64) /Bar\n",
        }
        cli_calls = []

        def fake_subprocess_run(cmd, *a, **k):
            if cmd[0] == "/usr/bin/dwarfdump":
                return MagicMock(stdout=dwarf_outputs.get(cmd[2], ""), returncode=0)
            if cmd[0] == self.cli_path:
                # `dsym uuid` is now used by parseDSYM (the Option-C
                # dSYM UUID subcommand). Route to a canned empty
                # JSON array so the fallback to dwarfdump fires
                # for the actual UUID extraction; cli_calls only
                # counts the debug-files upload invocations, which
                # is what these tests pin.
                if len(cmd) >= 3 and cmd[1] == "dsym" and cmd[2] == "uuid":
                    return MagicMock(stdout="[]", returncode=0)
                cli_calls.append(list(cmd))
                return MagicMock(returncode=0)
            raise AssertionError("unexpected subprocess: %r" % cmd)

        with patch.object(agent.subprocess, "run", side_effect=fake_subprocess_run):
            agent.main()

        self.assertEqual(cli_calls, [],
            "cache-hit dSYMs must not trigger any CLI exec; got %r" % cli_calls)

    def test_cache_only_updated_on_successful_upload(self):
        # If CLI returns non-zero, the UUIDs from that dSYM must NOT
        # land in ~/.bugseeUploadList — otherwise a failure would
        # silently block all retries.
        self._setup_module_state(version="1.0", build="1")
        dwarf_outputs = {
            os.path.join(self.dwarf_a, "Foo"):
                "UUID: AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA (arm64) /Foo\n",
            os.path.join(self.dwarf_b, "Bar"):
                "UUID: BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB (arm64) /Bar\n",
        }
        # A returns ok, B fails.
        def fake_subprocess_run(cmd, *a, **k):
            if cmd[0] == "/usr/bin/dwarfdump":
                return MagicMock(stdout=dwarf_outputs.get(cmd[2], ""), returncode=0)
            if cmd[0] == self.cli_path:
                # Distinguish by the dSYM path passed.
                if cmd[-1] == self.dsym_b:
                    return MagicMock(returncode=30)
                return MagicMock(returncode=0)
            raise AssertionError("unexpected: %r" % cmd)

        with patch.object(agent.subprocess, "run", side_effect=fake_subprocess_run):
            agent.main()

        cached = agent.loadUploadedList()
        self.assertIn("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", cached,
            "successful dSYM's UUID must persist to cache")
        self.assertNotIn("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", cached,
            "failed dSYM's UUID must NOT persist — next run should retry")

    def test_no_version_no_build_no_fallback_skips_upload(self):
        # When neither --version/--build nor any fallback supplies
        # values, the run must NOT attempt to exec CLI (which would
        # error out). Instead it logs and skips, leaving the cache
        # untouched so the next run can retry once values become
        # available.
        self._setup_module_state(version=None, build=None)
        # Make getVersionAndBuild return (None, None) too.
        dwarf_outputs = {
            os.path.join(self.dwarf_a, "Foo"):
                "UUID: AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA (arm64) /Foo\n",
            os.path.join(self.dwarf_b, "Bar"):
                "UUID: BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB (arm64) /Bar\n",
        }
        cli_calls = []

        def fake_subprocess_run(cmd, *a, **k):
            if cmd[0] == "/usr/bin/dwarfdump":
                return MagicMock(stdout=dwarf_outputs.get(cmd[2], ""), returncode=0)
            if cmd[0] == self.cli_path:
                # `dsym uuid` is now used by parseDSYM (the Option-C
                # dSYM UUID subcommand). Route to a canned empty
                # JSON array so the fallback to dwarfdump fires
                # for the actual UUID extraction; cli_calls only
                # counts the debug-files upload invocations, which
                # is what these tests pin.
                if len(cmd) >= 3 and cmd[1] == "dsym" and cmd[2] == "uuid":
                    return MagicMock(stdout="[]", returncode=0)
                cli_calls.append(list(cmd))
                return MagicMock(returncode=0)
            raise AssertionError("unexpected: %r" % cmd)

        with patch.object(agent.subprocess, "run", side_effect=fake_subprocess_run), \
             patch.object(agent, "getVersionAndBuild", return_value=(None, None)):
            agent.main()
        self.assertEqual(cli_calls, [],
            "no version/build means no CLI exec; got %r" % cli_calls)

    def test_fallback_to_getVersionAndBuild_when_flags_missing(self):
        # Flags missing, but getVersionAndBuild produces values (the
        # Xcode build-phase Info.plist path). Those values must reach
        # the CLI exec.
        self._setup_module_state(version=None, build=None)
        dwarf_out = (
            "UUID: AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA (arm64) /Foo\n"
        )
        cli_calls = []

        def fake_subprocess_run(cmd, *a, **k):
            if cmd[0] == "/usr/bin/dwarfdump":
                # Same UUID for both files — collapse to 1 upload via
                # cache-aware dedup-in-DSYM logic.
                return MagicMock(stdout=dwarf_out, returncode=0)
            if cmd[0] == self.cli_path:
                # Option-C: parseDSYM now prefers `bugsee-cli dsym
                # uuid`. Extract the UUID from the canned dwarf_out
                # so the CLI path produces the SAME UUIDs the
                # dwarfdump fallback would. cli_calls counts ONLY
                # the debug-files upload invocations.
                if len(cmd) >= 3 and cmd[1] == "dsym" and cmd[2] == "uuid":
                    import json as _json
                    m = re.search(r'UUID:\s+([0-9A-Fa-f-]+)', dwarf_out)
                    return MagicMock(
                        stdout=_json.dumps([m.group(1)] if m else []),
                        returncode=0,
                    )
                cli_calls.append(list(cmd))
                return MagicMock(returncode=0)
            raise AssertionError("unexpected: %r" % cmd)

        with patch.object(agent.subprocess, "run", side_effect=fake_subprocess_run), \
             patch.object(agent, "getVersionAndBuild", return_value=("9.9", "777")):
            agent.main()

        self.assertTrue(cli_calls, "expected at least one CLI invocation")
        # Every invocation must have the fallback values.
        for cmd in cli_calls:
            self.assertEqual(cmd[cmd.index("--version") + 1], "9.9")
            self.assertEqual(cmd[cmd.index("--build") + 1], "777")


# ──────────────────────────────────────────────────────────────────
# _upload_build_info_bundle — Phase D converged build-info upload via
# `bugsee-cli upload build-info --upload-url <url>` (pre-signed mode).
#
# The plugin already registered the build, so the CLI just PUTs the
# bundle to the signed URL. deps_gz / timings_gz are the gzipped
# per-blob payloads; the helper gunzips them into temp
# `dependencies.json` / `timings.json` (RAW JSON — the CLI does the
# zstd packing, the worker re-gzips on store). The tempdir is deleted
# in `finally`, so any assertion about the written bytes MUST happen
# inside the subprocess.run side_effect (while the files still exist).
# ──────────────────────────────────────────────────────────────────
class TestUploadBuildInfoBundle(unittest.TestCase):
    def setUp(self):
        # The helper reads `options.cli_path` / `options.cli_version`
        # via getattr with a None default; a bare MagicMock would make
        # those attributes truthy MagicMocks, so pin them explicitly.
        self.options = MagicMock()
        self.options.cli_path = None
        self.options.cli_version = None
        agent.options = self.options

    def test_argv_shape_and_gunzipped_bytes_both_present(self):
        deps_raw = b'{"dependencies":[{"name":"Alamofire"}]}'
        timings_raw = b'{"tasks":[{"name":"CompileSwift","ms":1234}]}'
        deps_gz = gzip.compress(deps_raw)
        timings_gz = gzip.compress(timings_raw)

        captured = {}

        def fake_run(argv, **kwargs):
            # The tempdir is wiped in finally; read the files NOW.
            captured['argv'] = list(argv)
            deps_idx = argv.index('--deps')
            timings_idx = argv.index('--timings')
            with open(argv[deps_idx + 1], 'rb') as fp:
                captured['deps_bytes'] = fp.read()
            with open(argv[timings_idx + 1], 'rb') as fp:
                captured['timings_bytes'] = fp.read()
            return MagicMock(returncode=0, stderr='')

        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run', side_effect=fake_run):
            ok = agent._upload_build_info_bundle(
                'https://signed.example/build-info', deps_gz, timings_gz)

        self.assertTrue(ok)
        argv = captured['argv']
        # The first five tokens are fixed; the --deps/--timings file
        # paths are tempdir-relative so assert structurally.
        self.assertEqual(argv[:5],
                         ['/cli', 'upload', 'build-info', '--upload-url',
                          'https://signed.example/build-info'])
        deps_idx = argv.index('--deps')
        timings_idx = argv.index('--timings')
        self.assertTrue(argv[deps_idx + 1].endswith('dependencies.json'))
        self.assertTrue(argv[timings_idx + 1].endswith('timings.json'))
        # Exactly the 9 tokens, nothing extra.
        self.assertEqual(len(argv), 9)
        # The CLI must see RAW (gunzipped) JSON, not the gzip bytes.
        self.assertEqual(captured['deps_bytes'], deps_raw)
        self.assertEqual(captured['timings_bytes'], timings_raw)

    def test_argv_full_equality_both_present(self):
        # Stronger pin: with deterministic file paths captured from the
        # side_effect, the entire argv equals the expected list.
        deps_gz = gzip.compress(b'{"d":1}')
        timings_gz = gzip.compress(b'{"t":2}')
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = list(argv)
            return MagicMock(returncode=0, stderr='')

        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run', side_effect=fake_run):
            agent._upload_build_info_bundle('URL', deps_gz, timings_gz)

        argv = captured['argv']
        deps_path = argv[argv.index('--deps') + 1]
        timings_path = argv[argv.index('--timings') + 1]
        self.assertEqual(argv, [
            '/cli', 'upload', 'build-info', '--upload-url', 'URL',
            '--deps', deps_path,
            '--timings', timings_path,
        ])

    def test_deps_only_omits_timings_flag(self):
        deps_raw = b'{"only":"deps"}'
        deps_gz = gzip.compress(deps_raw)
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = list(argv)
            deps_idx = argv.index('--deps')
            with open(argv[deps_idx + 1], 'rb') as fp:
                captured['deps_bytes'] = fp.read()
            return MagicMock(returncode=0, stderr='')

        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run', side_effect=fake_run):
            ok = agent._upload_build_info_bundle('URL', deps_gz, None)

        self.assertTrue(ok)
        argv = captured['argv']
        self.assertIn('--deps', argv)
        self.assertNotIn('--timings', argv)
        self.assertEqual(captured['deps_bytes'], deps_raw)

    def test_timings_only_omits_deps_flag(self):
        timings_raw = b'{"only":"timings"}'
        timings_gz = gzip.compress(timings_raw)
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = list(argv)
            timings_idx = argv.index('--timings')
            with open(argv[timings_idx + 1], 'rb') as fp:
                captured['timings_bytes'] = fp.read()
            return MagicMock(returncode=0, stderr='')

        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run', side_effect=fake_run):
            ok = agent._upload_build_info_bundle('URL', None, timings_gz)

        self.assertTrue(ok)
        argv = captured['argv']
        self.assertIn('--timings', argv)
        self.assertNotIn('--deps', argv)
        self.assertEqual(captured['timings_bytes'], timings_raw)

    def test_returns_false_on_nonzero_exit(self):
        deps_gz = gzip.compress(b'{}')
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          return_value=MagicMock(returncode=30, stderr='')):
            self.assertFalse(
                agent._upload_build_info_bundle('URL', deps_gz, None))

    def test_returns_true_on_zero_exit(self):
        deps_gz = gzip.compress(b'{}')
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          return_value=MagicMock(returncode=0, stderr='')):
            self.assertTrue(
                agent._upload_build_info_bundle('URL', deps_gz, None))

    def test_returns_false_when_no_cli_and_subprocess_not_called(self):
        deps_gz = gzip.compress(b'{}')
        with patch.object(agent, 'resolveCli', return_value=None), \
             patch.object(agent.subprocess, 'run') as mock_run:
            self.assertFalse(
                agent._upload_build_info_bundle('URL', deps_gz, None))
            mock_run.assert_not_called()

    def test_returns_false_on_subprocess_exception(self):
        deps_gz = gzip.compress(b'{}')
        with patch.object(agent, 'resolveCli', return_value='/cli'), \
             patch.object(agent.subprocess, 'run',
                          side_effect=OSError("ENOEXEC")):
            self.assertFalse(
                agent._upload_build_info_bundle('URL', deps_gz, None))


# ──────────────────────────────────────────────────────────────────
# _run_dependencies_pipeline routing — bundle vs legacy per-blob PUTs.
#
# We mock the collectors + registration so the test focuses purely on
# the branch: does the function ship one bundle (Phase D) or two legacy
# gzip PUTs? The bundle is chosen when the server signs a
# `build_info_upload_endpoint`, the BUGSEE_LEGACY_BUILDINFO_GZIP escape
# hatch is OFF, and at least one of deps/timings is present.
# ──────────────────────────────────────────────────────────────────
class TestRunDependenciesPipelineRouting(unittest.TestCase):
    def setUp(self):
        self.options = MagicMock()
        self.options.collect_deps = True
        self.options.collect_timings = True
        self.options.endpoint = 'https://apidev.bugsee.com'
        self.options.project_root = '/proj'
        self.options.cli_path = None
        self.options.cli_version = None
        agent.options = self.options

    def _patches(self, registration_response):
        """Common collector/registration mocks. Deps + timings both
        present so the routing decision is the only variable."""
        return [
            patch.object(agent, '_collect_all_dependencies',
                         return_value=([{'name': 'A'}], 'all', False)),
            patch.object(agent, 'resolve_build_timings',
                         return_value=({'total_ms': 10}, gzip.compress(b'{"t":1}'))),
            patch.object(agent, '_collect_build_metadata', return_value={}),
            patch.object(agent, '_extract_first_dwarf_uuid', return_value=None),
            patch.object(agent, '_build_dependencies_payload',
                         return_value=({'total': 1}, {'dependencies': [{'name': 'A'}]})),
            patch.object(agent, '_request_build_registration',
                         return_value=registration_response),
        ]

    def test_bundle_used_when_endpoint_present(self):
        resp = {
            'build_info_upload_endpoint': 'https://signed/build-info',
            'dependencies_upload_endpoint': 'https://signed/deps',
            'timings_upload_endpoint': 'https://signed/timings',
        }
        bundle = MagicMock(return_value=True)
        deps_put = MagicMock(return_value=True)
        timings_put = MagicMock(return_value=True)
        ctx = self._patches(resp) + [
            patch.object(agent, '_upload_build_info_bundle', bundle),
            patch.object(agent, '_put_dependencies_blob', deps_put),
            patch.object(agent, '_put_timings_blob', timings_put),
            patch.dict(agent.os.environ, {}, clear=False),
        ]
        # Ensure the escape hatch is OFF.
        with patch.dict(agent.os.environ,
                        {'BUGSEE_LEGACY_BUILDINFO_GZIP': ''}, clear=False):
            for p in ctx:
                p.start()
            try:
                ok = agent._run_dependencies_pipeline('tok', '/proj')
            finally:
                for p in reversed(ctx):
                    p.stop()

        self.assertTrue(ok)
        bundle.assert_called_once()
        # First positional arg is the signed build-info endpoint.
        self.assertEqual(bundle.call_args[0][0], 'https://signed/build-info')
        # Legacy per-blob PUTs MUST be skipped when the bundle wins.
        deps_put.assert_not_called()
        timings_put.assert_not_called()

    def test_legacy_used_when_endpoint_absent(self):
        resp = {
            'dependencies_upload_endpoint': 'https://signed/deps',
            'timings_upload_endpoint': 'https://signed/timings',
        }
        bundle = MagicMock(return_value=True)
        deps_put = MagicMock(return_value=True)
        timings_put = MagicMock(return_value=True)
        ctx = self._patches(resp) + [
            patch.object(agent, '_upload_build_info_bundle', bundle),
            patch.object(agent, '_put_dependencies_blob', deps_put),
            patch.object(agent, '_put_timings_blob', timings_put),
        ]
        with patch.dict(agent.os.environ,
                        {'BUGSEE_LEGACY_BUILDINFO_GZIP': ''}, clear=False):
            for p in ctx:
                p.start()
            try:
                ok = agent._run_dependencies_pipeline('tok', '/proj')
            finally:
                for p in reversed(ctx):
                    p.stop()

        self.assertTrue(ok)
        bundle.assert_not_called()
        deps_put.assert_called_once()
        timings_put.assert_called_once()

    def test_legacy_used_when_escape_hatch_set(self):
        resp = {
            'build_info_upload_endpoint': 'https://signed/build-info',
            'dependencies_upload_endpoint': 'https://signed/deps',
            'timings_upload_endpoint': 'https://signed/timings',
        }
        bundle = MagicMock(return_value=True)
        deps_put = MagicMock(return_value=True)
        timings_put = MagicMock(return_value=True)
        ctx = self._patches(resp) + [
            patch.object(agent, '_upload_build_info_bundle', bundle),
            patch.object(agent, '_put_dependencies_blob', deps_put),
            patch.object(agent, '_put_timings_blob', timings_put),
        ]
        with patch.dict(agent.os.environ,
                        {'BUGSEE_LEGACY_BUILDINFO_GZIP': '1'}, clear=False):
            for p in ctx:
                p.start()
            try:
                ok = agent._run_dependencies_pipeline('tok', '/proj')
            finally:
                for p in reversed(ctx):
                    p.stop()

        self.assertTrue(ok)
        # Escape hatch forces the legacy path even though the endpoint
        # was signed.
        bundle.assert_not_called()
        deps_put.assert_called_once()
        timings_put.assert_called_once()

    def test_request_build_info_upload_flag_set_in_body(self):
        # The registration body must opt into the bundle so the server
        # knows to sign the endpoint. Capture the JSON the agent POSTs.
        captured = {}

        def fake_register(app_token, endpoint, body_json):
            captured['body'] = json.loads(body_json)
            return {'build_info_upload_endpoint': 'https://signed/build-info'}

        ctx = [
            patch.object(agent, '_collect_all_dependencies',
                         return_value=([{'name': 'A'}], 'all', False)),
            patch.object(agent, 'resolve_build_timings',
                         return_value=({'total_ms': 10}, gzip.compress(b'{"t":1}'))),
            patch.object(agent, '_collect_build_metadata', return_value={}),
            patch.object(agent, '_extract_first_dwarf_uuid', return_value=None),
            patch.object(agent, '_build_dependencies_payload',
                         return_value=({'total': 1}, {'dependencies': []})),
            patch.object(agent, '_request_build_registration',
                         side_effect=fake_register),
            patch.object(agent, '_upload_build_info_bundle',
                         return_value=True),
        ]
        with patch.dict(agent.os.environ,
                        {'BUGSEE_LEGACY_BUILDINFO_GZIP': ''}, clear=False):
            for p in ctx:
                p.start()
            try:
                agent._run_dependencies_pipeline('tok', '/proj')
            finally:
                for p in reversed(ctx):
                    p.stop()

        self.assertTrue(captured['body'].get('request_build_info_upload'))


# ──────────────────────────────────────────────────────────────────
# CLI version-string validation. The same X.Y.Z[-prerelease] regex
# gates BUGSEE_CLI_VERSION before it flows into a download URL or the
# on-disk cache path (path-traversal / URL-injection guard). Version
# DISCOVERY is no longer hand-rolled here — the CLI's own `update`
# command owns it (see TestSelfUpdate / TestResolveCliSelfUpdateWiring).
# ──────────────────────────────────────────────────────────────────
class TestCliVersionValidation(unittest.TestCase):
    def test_default_version_is_floor(self):
        # Pin the download floor so a silent bump surfaces in review.
        self.assertEqual(agent.BUGSEE_CLI_DEFAULT_VERSION, "0.6.0")

    def test_accepts_plain_semver_and_prerelease(self):
        self.assertTrue(agent._BUGSEE_CLI_VERSION_RE.fullmatch("0.5.0"))
        self.assertTrue(agent._BUGSEE_CLI_VERSION_RE.fullmatch("1.2.3-rc.1"))

    def test_rejects_path_traversal_and_slash(self):
        self.assertIsNone(
            agent._BUGSEE_CLI_VERSION_RE.fullmatch("1.0/../evil"))
        self.assertIsNone(agent._BUGSEE_CLI_VERSION_RE.fullmatch("1/2/3"))
        self.assertIsNone(agent._BUGSEE_CLI_VERSION_RE.fullmatch("garbage"))


# ──────────────────────────────────────────────────────────────────
# CLI self-update — `_maybe_self_update` shells `bugsee-cli update
# --max-age 12h` BEST-EFFORT on a binary the agent manages. The CLI
# owns version discovery, download + verify, in-place self-replace,
# and the ~12h throttle. This helper must: fire the right argv, run at
# most once per process, skip when disabled, and swallow EVERY failure
# (timeout / non-zero exit / exec error) without raising.
# ──────────────────────────────────────────────────────────────────
class TestSelfUpdate(unittest.TestCase):
    def setUp(self):
        self._old_au = os.environ.get("BUGSEE_CLI_AUTO_UPDATE")
        os.environ.pop("BUGSEE_CLI_AUTO_UPDATE", None)
        agent._self_update_done = False

    def tearDown(self):
        agent._self_update_done = False
        if self._old_au is None:
            os.environ.pop("BUGSEE_CLI_AUTO_UPDATE", None)
        else:
            os.environ["BUGSEE_CLI_AUTO_UPDATE"] = self._old_au

    def test_fires_update_with_max_age_12h(self):
        with patch.object(agent.subprocess, "run") as run_mock:
            agent._maybe_self_update("/managed/bugsee-cli")
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        self.assertEqual(
            argv, ["/managed/bugsee-cli", "update", "--max-age", "12h"])
        # A generous timeout + non-raising exit-code policy.
        kwargs = run_mock.call_args[1]
        self.assertEqual(kwargs.get("timeout"), 120)
        self.assertFalse(kwargs.get("check"))
        # Never leak the CLI's stdout into the build log.
        self.assertEqual(kwargs.get("stdout"), agent.subprocess.DEVNULL)

    def test_runs_at_most_once_per_process(self):
        with patch.object(agent.subprocess, "run") as run_mock:
            agent._maybe_self_update("/managed/bugsee-cli")
            agent._maybe_self_update("/managed/bugsee-cli")
            agent._maybe_self_update("/managed/bugsee-cli")
        self.assertEqual(run_mock.call_count, 1)

    def test_skips_when_auto_update_disabled(self):
        os.environ["BUGSEE_CLI_AUTO_UPDATE"] = "off"
        with patch.object(agent.subprocess, "run") as run_mock:
            agent._maybe_self_update("/managed/bugsee-cli")
        run_mock.assert_not_called()

    def test_skips_when_cli_path_falsy(self):
        with patch.object(agent.subprocess, "run") as run_mock:
            agent._maybe_self_update(None)
        run_mock.assert_not_called()

    def test_swallows_timeout(self):
        with patch.object(
                agent.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="x", timeout=120)):
            # Must NOT raise.
            agent._maybe_self_update("/managed/bugsee-cli")

    def test_swallows_arbitrary_exception(self):
        with patch.object(agent.subprocess, "run",
                          side_effect=OSError("exec failed")):
            agent._maybe_self_update("/managed/bugsee-cli")

    def test_nonzero_exit_is_ignored(self):
        # A non-zero return code never raises (check=False) and is not
        # inspected — the binary is still considered usable.
        with patch.object(agent.subprocess, "run",
                          return_value=MagicMock(returncode=7)):
            agent._maybe_self_update("/managed/bugsee-cli")  # no raise


# ──────────────────────────────────────────────────────────────────
# Self-update wiring into `_resolveCli_uncached`. After the agent
# resolves a binary via the DOWNLOAD path (managed binary), it must
# run `_maybe_self_update` on it. It must NOT self-update a PATH /
# explicit BUGSEE_CLI_PATH binary, and must skip self-update entirely
# when auto-update is disabled.
# ──────────────────────────────────────────────────────────────────
class TestResolveCliSelfUpdateWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(self.fake_home, exist_ok=True)
        self._old_env = {
            k: os.environ.get(k) for k in (
                "HOME", "BUGSEE_CLI_PATH", "BUGSEE_CLI_VERSION",
                "BUGSEE_CLI_AUTO_UPDATE")
        }
        os.environ["HOME"] = self.fake_home
        for k in ("BUGSEE_CLI_PATH", "BUGSEE_CLI_VERSION",
                  "BUGSEE_CLI_AUTO_UPDATE"):
            os.environ.pop(k, None)
        agent._resolveCli_cache.clear()
        agent._self_update_done = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        agent._self_update_done = False
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _make_executable(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)

    def test_self_update_called_on_downloaded_binary(self):
        # No explicit path → the agent downloads the floor into the
        # managed cache dir, then self-updates THAT binary.
        triple = "aarch64-apple-darwin"
        cache_dir = os.path.join(
            self.fake_home, ".bugsee", "cli",
            agent.BUGSEE_CLI_DEFAULT_VERSION, triple)
        cache_bin = os.path.join(cache_dir, "bugsee-cli")

        def fake_download(version, target_triple, target_dir):
            self._make_executable(cache_bin)

        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli", side_effect=fake_download), \
             patch.object(agent, "_maybe_self_update") as su_mock:
            self.assertEqual(agent.resolveCli(), cache_bin)
        su_mock.assert_called_once_with(cache_bin)

    def test_self_update_called_on_cache_hit_managed_binary(self):
        # A previously-downloaded (managed) binary in the cache is also
        # eligible for self-update on this run.
        triple = "aarch64-apple-darwin"
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli",
            agent.BUGSEE_CLI_DEFAULT_VERSION, triple, "bugsee-cli")
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli") as dl_mock, \
             patch.object(agent, "_maybe_self_update") as su_mock:
            self.assertEqual(agent.resolveCli(), cache_bin)
            dl_mock.assert_not_called()
        su_mock.assert_called_once_with(cache_bin)

    def test_self_update_not_called_for_explicit_cli_path(self):
        # An explicit BUGSEE_CLI_PATH binary is user/system-managed —
        # never mutate it.
        bin_path = os.path.join(self.tmp, "explicit-cli")
        self._make_executable(bin_path)
        os.environ["BUGSEE_CLI_PATH"] = bin_path
        with patch.object(agent, "_maybe_self_update") as su_mock:
            self.assertEqual(agent.resolveCli(), bin_path)
        su_mock.assert_not_called()

    def test_self_update_not_called_for_cli_path_arg(self):
        bin_path = os.path.join(self.tmp, "arg-cli")
        self._make_executable(bin_path)
        with patch.object(agent, "_maybe_self_update") as su_mock:
            self.assertEqual(agent.resolveCli(cliPath=bin_path), bin_path)
        su_mock.assert_not_called()

    def test_downloaded_binary_returned_even_if_self_update_raises(self):
        # `_maybe_self_update` must already swallow everything, but pin
        # that a throwing self-update can't break resolution: the
        # downloaded binary is still returned.
        triple = "aarch64-apple-darwin"
        cache_dir = os.path.join(
            self.fake_home, ".bugsee", "cli",
            agent.BUGSEE_CLI_DEFAULT_VERSION, triple)
        cache_bin = os.path.join(cache_dir, "bugsee-cli")

        def fake_download(version, target_triple, target_dir):
            self._make_executable(cache_bin)

        # The real _maybe_self_update over a subprocess.run that raises —
        # resolution must still succeed.
        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli", side_effect=fake_download), \
             patch.object(agent.subprocess, "run",
                          side_effect=OSError("update exec failed")):
            self.assertEqual(agent.resolveCli(), cache_bin)

    def test_disabled_auto_update_skips_self_update_subprocess(self):
        # With auto-update OFF, the download still happens (explicit-path
        # / floor download is independent), but the self-update
        # subprocess must NOT fire.
        os.environ["BUGSEE_CLI_AUTO_UPDATE"] = "0"
        triple = "aarch64-apple-darwin"
        cache_bin = os.path.join(
            self.fake_home, ".bugsee", "cli",
            agent.BUGSEE_CLI_DEFAULT_VERSION, triple, "bugsee-cli")
        self._make_executable(cache_bin)
        with patch.object(agent, "detectHostTriple", return_value=triple), \
             patch.object(agent, "_downloadCli") as dl_mock, \
             patch.object(agent.subprocess, "run") as run_mock:
            self.assertEqual(agent.resolveCli(), cache_bin)
            dl_mock.assert_not_called()
            # _maybe_self_update ran (cache hit) but bailed before exec
            # because auto-update is disabled.
            run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for the iOS artefact upload flow in the fastlane BugseeAgent.

`run_artifact_upload_flow` packages a `.app` into a synthetic `.ipa`
and uploads it to the Bugsee back-end for size analysis. Mirror of
the SDK BugseeAgent's `run_size_analysis_flow`, minus the
chunked-upload opt-in and the in-build size-check evaluation. Same
build-registration payload shape so the back-end persists both
producers identically.

Coverage focuses on the network-free pieces:
  - Bad input validation (missing path, non-directory, wrong extension)
  - Payload shape (request_artifact_upload, format, artifact_size, etc.)
  - Presigned PUT round-trip (mocked urllib)
  - Build-info-only path (request_artifact_upload=False)
"""

from __future__ import annotations

import importlib.util
import importlib.machinery
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, '..', 'BugseeAgent'))


def _load_agent_module():
    loader = importlib.machinery.SourceFileLoader("BugseeAgent", AGENT_PATH)
    spec = importlib.util.spec_from_loader("BugseeAgent", loader, origin=AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


agent = _load_agent_module()


def _make_app(parent):
    app = os.path.join(parent, 'Foo.app')
    os.makedirs(app)
    with open(os.path.join(app, 'Info.plist'), 'w') as f:
        f.write('<plist><dict><key>X</key><string>Y</string></dict></plist>')
    with open(os.path.join(app, 'Foo'), 'wb') as f:
        f.write(b'macho-stub-payload')
    os.chmod(os.path.join(app, 'Foo'), 0o755)
    return app


class _FakeHttpResponse:
    """Minimal urllib.response stand-in supporting `with` + `.status`."""

    def __init__(self, body=b'', status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


def _ok_envelope(presigned_url=None, build_id="build-123"):
    """Mimic the appserver's `{ok:true, result:{...}}` shape."""
    result = {"build_id": build_id}
    if presigned_url is not None:
        result["endpoint"] = presigned_url
    return json.dumps({"ok": True, "result": result}).encode('utf-8')


class TestRunArtifactUploadFlowValidation(unittest.TestCase):
    """Input-validation pins — these must short-circuit before any
    network activity so a bad fastlane lane config doesn't burn CI
    time."""

    def test_returns_false_when_app_path_is_none(self):
        with mock.patch('urllib.request.urlopen') as urlopen:
            ok = agent.run_artifact_upload_flow(
                'token', 'https://api.bugsee.com', None)
        self.assertFalse(ok)
        urlopen.assert_not_called()

    def test_returns_false_when_app_path_does_not_exist(self):
        with mock.patch('urllib.request.urlopen') as urlopen:
            ok = agent.run_artifact_upload_flow(
                'token', 'https://api.bugsee.com', '/no/such/path.app')
        self.assertFalse(ok)
        urlopen.assert_not_called()

    def test_returns_false_when_app_path_is_not_an_app(self):
        tmp = tempfile.mkdtemp()
        try:
            not_an_app = os.path.join(tmp, 'NotAnApp.bundle')
            os.makedirs(not_an_app)
            with mock.patch('urllib.request.urlopen') as urlopen:
                ok = agent.run_artifact_upload_flow(
                    'token', 'https://api.bugsee.com', not_an_app)
            self.assertFalse(ok)
            urlopen.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRunArtifactUploadFlowHappyPath(unittest.TestCase):
    """Round-trip the full flow with urllib mocked. Pins the
    registration payload shape AND the presigned-PUT call sequence."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = _make_app(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_request_artifact_upload_path_invokes_presigned_put(self):
        # First urlopen call → registration POST returning a
        # presigned URL. Second call → artefact PUT to that URL.
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append((req.full_url, req.get_method(),
                              dict(req.header_items())))
            if 'apps/' in req.full_url:
                return _FakeHttpResponse(
                    body=_ok_envelope(presigned_url='https://s3/put'))
            return _FakeHttpResponse(status=200)

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent.run_artifact_upload_flow(
                'app-token', 'https://api.bugsee.com', self.app,
                version='1.2.3', build_number='42',
                request_artifact_upload=True,
            )
        self.assertTrue(ok)
        # Two HTTP calls — registration then PUT.
        self.assertEqual(len(captured), 2)
        reg_url, reg_method, reg_headers = captured[0]
        put_url, put_method, put_headers = captured[1]
        # Registration is a POST to /v2/apps/<token>/builds.
        self.assertEqual(reg_method, 'POST')
        self.assertIn('/v2/apps/app-token/builds', reg_url)
        self.assertEqual(reg_headers.get('Content-type'), 'application/json')
        # PUT lands at the presigned URL with octet-stream content type
        # and a precise Content-Length (S3 signature requirement).
        self.assertEqual(put_method, 'PUT')
        self.assertEqual(put_url, 'https://s3/put')
        self.assertEqual(put_headers.get('Content-type'),
                         'application/octet-stream')
        # Content-Length must be a numeric string.
        cl = put_headers.get('Content-length')
        self.assertIsNotNone(cl)
        self.assertTrue(cl.isdigit(), 'Content-Length must be numeric, got %r' % cl)
        self.assertGreater(int(cl), 0)

    def test_registration_payload_carries_expected_fields(self):
        captured_body = []

        def fake_urlopen(req, timeout=None):
            if 'apps/' in req.full_url:
                # Capture the JSON body from the POST.
                data = req.data
                captured_body.append(json.loads(data.decode('utf-8')))
                return _FakeHttpResponse(
                    body=_ok_envelope(presigned_url='https://s3/put'))
            return _FakeHttpResponse(status=200)

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent.run_artifact_upload_flow(
                'app-token', 'https://api.bugsee.com', self.app,
                version='1.2.3', build_number='42',
                request_artifact_upload=True,
            )
        self.assertTrue(ok)
        self.assertEqual(len(captured_body), 1)
        payload = captured_body[0]
        # Required fields the back-end's _register_build route expects.
        self.assertEqual(payload['format'], 'ipa')
        self.assertEqual(payload['version'], '1.2.3')
        self.assertEqual(payload['build'], '42')
        self.assertEqual(payload['request_artifact_upload'], True)
        # artifact_size is the synthetic IPA's on-disk size after
        # packaging; must be > 0 for a non-empty .app.
        self.assertIn('artifact_size', payload)
        self.assertGreater(payload['artifact_size'], 0)

    def test_build_info_only_path_skips_artefact_put(self):
        # request_artifact_upload=False: register the build (records
        # artifact_size for the size-trend chart) but DON'T ship the
        # bytes. Pin: only ONE urlopen call (the registration POST).
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.get_method())
            return _FakeHttpResponse(body=_ok_envelope())

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent.run_artifact_upload_flow(
                'app-token', 'https://api.bugsee.com', self.app,
                version='1.2.3', build_number='42',
                request_artifact_upload=False,
            )
        self.assertTrue(ok)
        self.assertEqual(captured, ['POST'])

    def test_build_info_only_with_artifact_size_sent_but_no_put(self):
        # End-to-end pin for `--build-info-only` (Tier 3 follow-up).
        # The registration POST must still carry `artifact_size`
        # (so the dashboard's size-trend chart works), and
        # `request_artifact_upload` must be False, and the helper
        # must NOT attempt a PUT.
        captured_body = []
        captured_methods = []

        def fake_urlopen(req, timeout=None):
            captured_methods.append(req.get_method())
            if 'apps/' in req.full_url:
                captured_body.append(json.loads(req.data.decode('utf-8')))
                return _FakeHttpResponse(body=_ok_envelope())
            return _FakeHttpResponse(status=200)

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent.run_artifact_upload_flow(
                'app-token', 'https://api.bugsee.com', self.app,
                version='1.2.3', build_number='42',
                request_artifact_upload=False,
            )
        self.assertTrue(ok)
        self.assertEqual(captured_methods, ['POST'])
        self.assertEqual(len(captured_body), 1)
        payload = captured_body[0]
        # Size IS still recorded (the dashboard size-trend uses this).
        self.assertIn('artifact_size', payload)
        self.assertGreater(payload['artifact_size'], 0)
        # request_artifact_upload IS False so the server signs no PUT URL.
        self.assertEqual(payload['request_artifact_upload'], False)

    def test_registration_failure_returns_false_without_put(self):
        # Registration POST fails (e.g. invalid token). We MUST NOT
        # attempt the PUT — there's no presigned URL to PUT to.
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req.get_method())
            # `{ok:false,error:{...}}` envelope — _request_build_registration
            # returns None for this shape.
            return _FakeHttpResponse(body=json.dumps({
                "ok": False,
                "error": {"type": "invalid_token", "message": "nope"},
            }).encode('utf-8'))

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent.run_artifact_upload_flow(
                'bad-token', 'https://api.bugsee.com', self.app,
                request_artifact_upload=True,
            )
        self.assertFalse(ok)
        self.assertEqual(captured, ['POST'])

    def test_missing_presigned_url_returns_false(self):
        # Defensive: registration succeeded but the server returned
        # no `endpoint` field. Treat as a server bug + soft failure.
        with mock.patch('urllib.request.urlopen',
                        return_value=_FakeHttpResponse(body=_ok_envelope())):
            ok = agent.run_artifact_upload_flow(
                'app-token', 'https://api.bugsee.com', self.app,
                request_artifact_upload=True,
            )
        self.assertFalse(ok)


class TestPutArtifactBlob(unittest.TestCase):
    """Direct tests for the presigned-PUT helper."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ipa = os.path.join(self.tmp, 'Foo.ipa')
        with open(self.ipa, 'wb') as f:
            f.write(b'PK' + b'\x00' * 64)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_false_when_url_is_empty(self):
        self.assertFalse(agent._put_artifact_blob('', self.ipa))

    def test_returns_false_when_ipa_path_missing(self):
        self.assertFalse(agent._put_artifact_blob(
            'https://s3/put', '/no/such/path.ipa'))

    def test_sends_octet_stream_and_content_length(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['method'] = req.get_method()
            captured['url'] = req.full_url
            captured['headers'] = dict(req.header_items())
            return _FakeHttpResponse(status=200)

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok = agent._put_artifact_blob('https://s3/put', self.ipa)
        self.assertTrue(ok)
        self.assertEqual(captured['method'], 'PUT')
        self.assertEqual(captured['url'], 'https://s3/put')
        self.assertEqual(
            captured['headers']['Content-type'], 'application/octet-stream')
        cl = captured['headers'].get('Content-length')
        self.assertEqual(int(cl), os.path.getsize(self.ipa))

    def test_returns_false_on_nonzero_http_status(self):
        with mock.patch('urllib.request.urlopen',
                        return_value=_FakeHttpResponse(status=500)):
            self.assertFalse(agent._put_artifact_blob('https://s3/put', self.ipa))


if __name__ == '__main__':
    unittest.main()

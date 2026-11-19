import base64
import json
import os
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import mock

import pyzipper
from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .api_views import validate_generate_params
from .forms import GenerateForm
from .views import get_full_url, _norm_hex_color


class ThemeColorValidationTests(SimpleTestCase):
    base = {
        'platform': 'windows',
        'version': '1.5.3',
        'exename': 'client',
        'direction': 'both',
        'installation': 'installationY',
        'settings': 'settingsY',
        'theme': 'system',
        'themeDorO': 'default',
        'passApproveMode': 'password-click',
        'permissionsType': 'custom',
    }

    def _form(self, **overrides):
        data = dict(self.base)
        data.update(overrides)
        return GenerateForm(data)

    def test_theme_colors_accept_hash_or_bare_hex(self):
        form = self._form(themeColor='ff8800', themeSurfaceLight='#FFF6E9')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['themeColor'], '#FF8800')
        self.assertEqual(form.cleaned_data['themeSurfaceLight'], '#FFF6E9')

    def test_blank_theme_color_means_stock(self):
        form = self._form(themeColor='', themeSurfaceDark='   ')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['themeColor'], '')
        self.assertEqual(form.cleaned_data['themeSurfaceDark'], '')

    def test_bad_theme_color_is_rejected(self):
        for value in ('red', '#ff880', '#gg8800', '#ff8800ff'):
            with self.subTest(value=value):
                form = self._form(themeColor=value)
                self.assertFalse(form.is_valid())
                self.assertIn('themeColor', form.errors)

    def test_api_normalizes_theme_colors(self):
        data = dict(self.base)
        data.update({
            'themeColor': 'ff8800',
            'themeSurfaceLight': '#FFF6E9',
            'themeSurfaceDark': '',
            'themeMeColor': '#00AA66',
        })
        cleaned, errors = validate_generate_params(data)
        self.assertEqual(errors, {})
        self.assertEqual(cleaned['themeColor'], '#FF8800')
        self.assertEqual(cleaned['themeSurfaceLight'], '#FFF6E9')
        self.assertEqual(cleaned['themeSurfaceDark'], '')
        self.assertEqual(cleaned['themeMeColor'], '#00AA66')

    def test_api_rejects_bad_theme_color(self):
        data = dict(self.base)
        data['themeMeColor'] = 'green'
        _, errors = validate_generate_params(data)
        self.assertIn('themeMeColor', errors)

    def test_norm_hex_color(self):
        self.assertEqual(_norm_hex_color('ff8800'), '#FF8800')
        self.assertEqual(_norm_hex_color('#ff8800'), '#FF8800')
        self.assertEqual(_norm_hex_color(''), '')
        self.assertEqual(_norm_hex_color('nope'), '')


class GetFullUrlTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get('/', HTTP_HOST='127.0.0.1:8000')

    @override_settings(GENURL='', PROTOCOL='https')
    def test_uses_request_host_without_genurl(self):
        self.assertEqual(get_full_url(self.request), 'https://127.0.0.1:8000')

    @override_settings(GENURL='https://rdgen.example.com:4433/', PROTOCOL='https')
    def test_uses_genurl(self):
        self.assertEqual(get_full_url(self.request), 'https://rdgen.example.com:4433')

    @override_settings(GENURL='rdgen.example.com:8000', PROTOCOL='http')
    def test_adds_protocol_to_genurl_without_one(self):
        self.assertEqual(get_full_url(self.request), 'http://rdgen.example.com:8000')


@override_settings(GENURL='https://rdgen.example.com', PROTOCOL='https')
class GeneratorViewTests(TestCase):
    def setUp(self):
        cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory()
        os.chdir(tmp.name)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, cwd)

    def test_workflow_downloads_zip_from_genurl(self):
        github = mock.Mock(status_code=200)
        github.json.return_value = {'workflow_run_id': 1, 'html_url': ''}
        with mock.patch('rdgenerator.views.requests.post', return_value=github) as post:
            self.client.post('/generator', {
                'platform': 'windows',
                'version': '1.5.3',
                'exename': 'client',
                'direction': 'incoming',
                'installation': 'installationY',
                'settings': 'settingsY',
                'theme': 'system',
                'themeDorO': 'default',
                'passApproveMode': 'password-click',
                'permissionsType': 'custom',
            }, HTTP_HOST='127.0.0.1')

        post.assert_called_once()
        zip_url = json.loads(post.call_args.kwargs['json']['inputs']['zip_url'])
        self.assertEqual(zip_url['url'], 'https://rdgen.example.com')

        # The generated custom_.txt must hide RustDesk's "Powered by" link
        # through the built-in option, not by rewriting the translation.
        zip_path = os.path.join('temp_zips', zip_url['file'])
        with pyzipper.AESZipFile(zip_path) as zf:
            zf.setpassword(settings.ZIP_PASSWORD.encode())
            secrets = json.loads(zf.read('secrets.json'))
        custom = json.loads(base64.b64decode(secrets['custom']))
        self.assertEqual(custom['override-settings']['hide-powered-by-me'], 'Y')
        self.assertEqual(secrets['filename'], 'client_v1.5.3')
        self.assertEqual(custom['conn-type'], 'incoming')

class WindowsUpdateTests(SimpleTestCase):
    def setUp(self):
        from Cryptodome.PublicKey import ECC

        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        root = Path(storage.name)
        host = ECC.generate(curve="Ed25519")
        admin = ECC.generate(curve="Ed25519")
        self.msi = root / "fixture.msi"
        self.msi.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"installer fixture")
        self.signing_key = root / "host.pem"
        self.signing_key.write_text(host.export_key(format="PEM"), encoding="ascii")
        self.host_public = host.public_key().export_key(format="raw")
        options = override_settings(
            RDGEN_UPDATE_ROOT=str(root / "releases"),
            RDGEN_UPDATE_ORIGIN="https://rdgen.example.com:8443",
            RDGEN_UPDATE_BUILD_TOKEN="test-build-token",
            RDGEN_UPDATE_CHANNELS={
                "host": {"access": "a" * 64, "public_key": base64.b64encode(self.host_public).decode()},
                "admin": {
                    "access": "b" * 64,
                    "public_key": base64.b64encode(admin.public_key().export_key(format="raw")).decode(),
                },
            },
        )
        options.enable()
        self.addCleanup(options.disable)
        self.host_route = "/updates/host/" + "a" * 64
        self.admin_route = "/updates/admin/" + "b" * 64

    def test_signed_release_and_channel_isolation(self):
        import hashlib
        from Cryptodome.Signature import eddsa
        from rdgenerator.updates import SIGNATURE_CONTEXT, publish_release

        empty = self.client.post(self.host_route + "/api/version/latest")
        self.assertTrue(empty.json()["url"].endswith("/v0.0.1"))
        self.assertEqual(publish_release("host", "1.5.3+021", self.msi, self.signing_key), "v1.5.3+021")
        latest = self.client.post(self.host_route + "/api/version/latest")
        self.assertTrue(latest.json()["url"].endswith("/v1.5.3+021"))
        self.assertEqual(latest["Cache-Control"], "private, no-store")

        release = self.host_route + "/rustdesk/rustdesk/releases/download/v1.5.3+021/"
        with closing(self.client.get(release + "rustdesk-update.json")) as response:
            metadata_bytes = b"".join(response.streaming_content)
        with closing(self.client.get(release + "rustdesk-update.json.sig")) as response:
            signature = json.loads(b"".join(response.streaming_content))
        with closing(self.client.get(release + "rustdesk-x86_64.msi")) as response:
            actual = b"".join(response.streaming_content)
        eddsa.new(eddsa.import_public_key(self.host_public), "rfc8032").verify(
            SIGNATURE_CONTEXT + metadata_bytes, base64.b64decode(signature["signature"]),
        )
        self.assertEqual(actual, self.msi.read_bytes())
        self.assertEqual(json.loads(metadata_bytes)["artifacts"][0]["sha256"], hashlib.sha256(actual).hexdigest())
        self.assertEqual(self.client.post(self.host_route.replace("/host/", "/admin/") + "/api/version/latest").status_code, 404)
        self.assertEqual(self.client.get(release.replace(self.host_route, self.admin_route) + "rustdesk-x86_64.msi").status_code, 404)

    def test_publish_refuses_wrong_key_and_older_versions(self):
        from rdgenerator.updates import publish_release

        with self.assertRaisesRegex(ValueError, "does not match"):
            publish_release("admin", "1.5.3+021", self.msi, self.signing_key)
        publish_release("host", "1.5.3+021", self.msi, self.signing_key)
        with self.assertRaisesRegex(ValueError, "newer"):
            publish_release("host", "1.5.3+019", self.msi, self.signing_key)
        self.assertEqual(publish_release("host", "1.5.4", self.msi, self.signing_key), "v1.5.4")

    def test_build_token_required_before_dispatch(self):
        from rdgenerator.views import generate_custom_client

        params = {"windowsUpdateChannel": "host", "version": "1.5.3"}
        with mock.patch("rdgenerator.views.requests.post") as dispatch:
            self.assertEqual(generate_custom_client(params, "")["status_code"], 403)
            params["windowsUpdateBuildToken"] = "test-build-token"
            params["direction"] = "outgoing"
            self.assertEqual(generate_custom_client(params, "")["status_code"], 400)
            dispatch.assert_not_called()

    def test_authorized_update_build_uses_pinned_channel(self):
        from rdgenerator.views import generate_custom_client

        workspace = tempfile.TemporaryDirectory()
        cwd = os.getcwd()
        self.addCleanup(workspace.cleanup)
        self.addCleanup(os.chdir, cwd)
        os.chdir(workspace.name)
        response = mock.Mock(status_code=401)
        with mock.patch("rdgenerator.views.requests.post", return_value=response) as dispatch:
            generate_custom_client({
                "windowsUpdateChannel": "host",
                "windowsUpdateBuildToken": "test-build-token",
                "version": "1.5.3",
                "app_version": "1.5.3+001",
                "direction": "incoming",
                "exename": "host",
                "overrideManual": "allow-auto-update=N",
            }, "https://rdgen.example.com:8443")
        inputs = dispatch.call_args.kwargs["json"]["inputs"]
        self.assertEqual(inputs["app_version"], "1.5.3+001")
        zipname = json.loads(inputs["zip_url"])["file"]
        with pyzipper.AESZipFile(Path("temp_zips") / zipname) as archive:
            archive.setpassword(settings.ZIP_PASSWORD.encode())
            variables = json.loads(archive.read("secrets.json"))
        custom = json.loads(base64.b64decode(variables["custom"]))
        self.assertEqual(variables["filename"], "host_v1.5.3-build001")
        self.assertEqual(variables["rdgen_update_base"], "https://rdgen.example.com:8443" + self.host_route)
        self.assertEqual(custom["override-settings"]["allow-auto-update"], "Y")
        self.assertNotIn("test-build-token", json.dumps(variables))

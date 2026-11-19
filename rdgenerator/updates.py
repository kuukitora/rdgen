"""Serve and publish signed Windows update releases for installed RDGen clients."""

import base64
import hashlib
import json
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from Cryptodome.PublicKey import ECC
from Cryptodome.Signature import eddsa
from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

UPDATE_CHANNELS = ("host", "admin")
FILES = ("rustdesk-update.json", "rustdesk-update.json.sig", "rustdesk-x86_64.msi")
SIGNATURE_CONTEXT = b"RustDesk update metadata v1\n"
VERSION = re.compile(r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,4})(?:\+([0-9]{3}))?")
CHANNEL_ACCESS = re.compile(r"[0-9a-f]{64}")


def version_order(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError("Use a version such as 1.5.3 or 1.5.3+001.")
    major, minor, patch, build = (int(part or 0) for part in match.groups())
    if major > 255 or minor > 255 or patch > 65535:
        raise ValueError("The version exceeds the Windows Installer limits.")
    if not any((major, minor, patch)):
        raise ValueError("An update version must be greater than zero.")
    return major, minor, patch, build


def channel_config(channel):
    if channel not in UPDATE_CHANNELS or not settings.RDGEN_UPDATE_ROOT:
        raise ValueError("The Windows update channel is unavailable.")
    origin = settings.RDGEN_UPDATE_ORIGIN.rstrip("/")
    url = urlsplit(origin)
    if (
        url.scheme != "https" or not url.hostname or url.username or url.password
        or url.path or url.query or url.fragment or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", url.netloc)
    ):
        raise ValueError("Set RDGEN_UPDATE_ORIGIN to an HTTPS origin.")
    if url.port == 0:
        raise ValueError("Set RDGEN_UPDATE_ORIGIN to a reachable HTTPS origin.")
    config = settings.RDGEN_UPDATE_CHANNELS.get(channel, {})
    access = config.get("access", "")
    public_key = config.get("public_key", "")
    try:
        raw_key = base64.b64decode(public_key, validate=True)
        eddsa.import_public_key(raw_key)
    except ValueError as exc:
        raise ValueError("The Windows update channel needs an Ed25519 public key.") from exc
    if len(raw_key) != 32 or not CHANNEL_ACCESS.fullmatch(access):
        raise ValueError("The Windows update channel is missing its key or access token.")
    other = "admin" if channel == "host" else "host"
    peer = settings.RDGEN_UPDATE_CHANNELS.get(other, {})
    if peer.get("access") == access or peer.get("public_key") == public_key:
        raise ValueError("Host and Admin require different update keys and access tokens.")
    return Path(settings.RDGEN_UPDATE_ROOT) / channel, f"{origin}/updates/{channel}/{access}", public_key


def authenticate(channel, access):
    try:
        directory, url, _ = channel_config(channel)
    except ValueError as exc:
        raise Http404 from exc
    expected = url.rsplit("/", 1)[1]
    if not secrets.compare_digest(expected, access):
        raise Http404
    return directory, url


def latest_release(directory):
    pointer = directory / "latest"
    if not pointer.is_file():
        return None
    release_id = pointer.read_text(encoding="ascii").strip()
    if not release_id.startswith("v"):
        raise ValueError("Invalid latest update release.")
    version_order(release_id[1:])
    if not all((directory / release_id / filename).is_file() for filename in FILES):
        raise ValueError("Incomplete latest update release.")
    return release_id


@csrf_exempt
@require_POST
def check_update(request, channel, access):
    directory, url = authenticate(channel, access)
    try:
        release_id = latest_release(directory)
    except ValueError as exc:
        raise Http404 from exc
    # RustDesk requires a syntactically valid release URL even for an empty channel.
    release_id = release_id or "v0.0.1"
    response = JsonResponse({"url": f"{url}/rustdesk/rustdesk/releases/tag/{release_id}"})
    response["Cache-Control"] = "private, no-store"
    return response


@require_http_methods(["GET", "HEAD"])
def release_file(request, channel, access, release_id, filename):
    directory, _ = authenticate(channel, access)
    if filename not in FILES or not release_id.startswith("v"):
        raise Http404
    try:
        version_order(release_id[1:])
        file = (directory / release_id / filename).open("rb")
    except (ValueError, FileNotFoundError) as exc:
        raise Http404 from exc
    response = FileResponse(file, as_attachment=True, filename=filename)
    response["Cache-Control"] = "private, no-store"
    return response


def publish_release(channel, version, msi, private_key):
    """Build an immutable release and make it discoverable after all files exist."""
    requested = version_order(version)
    directory, url, public_key = channel_config(channel)
    key = ECC.import_key(private_key.read_bytes())
    if (
        key.curve != "Ed25519" or not key.has_private()
        or key.public_key().export_key(format="raw") != base64.b64decode(public_key)
    ):
        raise ValueError("The signing key does not match the channel public key.")

    if msi.suffix.lower() != ".msi":
        raise ValueError("Publish a Windows MSI installation package.")

    directory.mkdir(parents=True, exist_ok=True)
    # Serialize publication so concurrent operators cannot move the channel backwards.
    lock = directory / ".publish-lock"
    lock.mkdir()
    try:
        current = latest_release(directory)
        if current and requested <= version_order(current[1:]):
            raise ValueError("An update must be newer than the current release.")
        release_id = f"v{version}"
        with tempfile.TemporaryDirectory(dir=directory) as staging:
            stage = Path(staging) / release_id
            stage.mkdir()
            digest = hashlib.sha256()
            size = 0
            with msi.open("rb") as source, (stage / FILES[2]).open("wb") as dest:
                magic = source.read(8)
                if magic != bytes.fromhex("d0cf11e0a1b11ae1"):
                    raise ValueError("The selected package is not a Windows Installer database.")
                source.seek(0)
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    dest.write(block)
                    digest.update(block)
                    size += len(block)
            metadata = {
                "schema_version": 1,
                "app": "rustdesk",
                "package_id": "rustdesk",
                "version": version,
                "release_id": release_id,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "signature_key_id": "custom-update",
                "artifacts": [{
                    "platform": "windows",
                    "arch": "x86_64",
                    "format": "msi",
                    "file_name": FILES[2],
                    "url": f"{url}/rustdesk/rustdesk/releases/download/{release_id}/{FILES[2]}",
                    "size": size,
                    "sha256": digest.hexdigest(),
                }],
            }
            payload = json.dumps(metadata, separators=(",", ":")).encode()
            signature = eddsa.new(key, "rfc8032").sign(SIGNATURE_CONTEXT + payload)
            (stage / FILES[0]).write_bytes(payload)
            (stage / FILES[1]).write_text(json.dumps({
                "schema_version": 1,
                "algorithm": "ed25519",
                "key_id": "custom-update",
                "signature": base64.b64encode(signature).decode(),
            }), encoding="ascii")
            target = directory / release_id
            if target.exists():
                raise ValueError("The release already exists.")
            stage.rename(target)
            (Path(staging) / "latest").write_text(release_id + "\n", encoding="ascii")
            (Path(staging) / "latest").replace(directory / "latest")
    finally:
        lock.rmdir()
    return release_id

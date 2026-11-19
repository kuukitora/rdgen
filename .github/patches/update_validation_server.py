"""Loopback-only HTTPS update feed for an isolated Windows runner.

The signed metadata is prepared by the release operator; no update signing
private key is needed on the runner. Request paths are never logged because
they contain the channel access token.
"""
import argparse
import datetime
import json
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def make_certificate(folder):
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'RDGen isolated update test CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'rdgen.kura.fun')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('rdgen.kura.fun')]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (folder / 'test-ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / 'test-server.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / 'test-server.key').write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def serve(folder, port=8443):
    metadata = json.loads((folder / 'rustdesk-update.json').read_bytes())
    artifact = metadata['artifacts'][0]['url']
    prefix, rest = artifact.split('/releases/download/', 1)
    release_id, _ = rest.split('/', 1)
    latest_url = prefix + '/releases/tag/' + release_id
    events = folder / 'requests.log'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def record(self, name):
            with events.open('a', encoding='utf-8') as file:
                file.write(name + '\n')

        def do_POST(self):
            if not urlsplit(self.path).path.endswith('/api/version/latest'):
                self.send_error(404)
                return
            length = min(int(self.headers.get('Content-Length', '0')), 65536)
            self.rfile.read(length)
            body = json.dumps({'url': latest_url}).encode()
            self.record('version')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            name = unquote(urlsplit(self.path).path.rsplit('/', 1)[-1])
            if name not in ('rustdesk-update.json', 'rustdesk-update.json.sig', 'rustdesk-x86_64.msi'):
                self.send_error(404)
                return
            file = folder / name
            if not file.is_file():
                self.send_error(404)
                return
            self.record(name)
            self.send_response(200)
            self.send_header('Content-Length', str(file.stat().st_size))
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            with file.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)

    server = HTTPServer(('127.0.0.1', port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'test-server.pem', folder / 'test-server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'serve'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--port', type=int, default=8443)
    args = parser.parse_args()
    if args.mode == 'prepare':
        make_certificate(args.directory)
    else:
        serve(args.directory, args.port)

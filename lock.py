"""A cache-first view of declared Simple API indexes; uv does the resolving."""
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from functools import lru_cache
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from platform import python_version
from threading import Thread, Lock
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import tomli_w
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


def loopback(url):
    parts = urlsplit(url)
    return parts.scheme == 'http' and parts.hostname == '127.0.0.1'


def require_transport(url, fixture=False):
    parts = urlsplit(url)
    if not parts.hostname or parts.username is not None or not (
        parts.scheme == 'https' or (fixture and loopback(url))
    ):
        raise ValueError(f'Expected HTTPS publisher URL (HTTP only for loopback fixtures): {url}')


class PublisherRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_transport(newurl, fixture=loopback(req.full_url))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def lock(requirements, pool, output, indexes):
    pool.mkdir(parents=True, exist_ok=True)
    for index in indexes.values():
        require_transport(index, fixture=loopback(index))
    opener = build_opener(PublisherRedirect())
    artifacts = {}
    writes = Lock()
    downloads = []
    failures = []
    tags = set(sys_tags())
    initial = {path.name for path in pool.iterdir()}

    @lru_cache(maxsize=None)
    def page(package):
        index = indexes.get(package, indexes['default']).rstrip('/') + '/'
        address = urljoin(index, package + '/')
        try:
            with opener.open(Request(address, headers={'Accept': 'text/html'}), timeout=60) as response:
                links = Links()
                links.feed(response.read().decode('utf-8'))
                base = response.geturl()
        except HTTPError as error:
            if error.code == 404:
                error.close()
                return []
            raise
        result = []
        for attrs in links.links:
            source = urljoin(base, attrs.get('href', ''))
            parts = urlsplit(source)
            filename = unquote(parts.path.rsplit('/', 1)[-1])
            hashes = parse_qs(parts.fragment).get('sha256', [])
            if not filename.endswith('.whl') or len(hashes) != 1:
                continue
            if '/' in filename or '\\' in filename or not re.fullmatch(r'[0-9a-f]{64}', hashes[0]):
                raise ValueError('Invalid artifact identity')
            require_transport(source, fixture=loopback(index))
            try:
                name, _, _, _ = parse_wheel_filename(filename)
            except InvalidWheelFilename:
                continue
            if name != package:
                raise ValueError(f'Wrong package in {address}: {filename}')
            checksum = hashes[0]
            url = parts._replace(fragment='').geturl()
            key = hashlib.sha256(f'{url}\n{checksum}'.encode()).hexdigest()
            metadata = attrs.get('data-core-metadata', attrs.get('data-dist-info-metadata'))
            artifacts[key] = (filename, checksum, url, metadata)
            result.append((key, attrs))
        return result

    @lru_cache(maxsize=None)
    def cached(filename, checksum):
        path = pool / filename
        return path.is_file() and not path.is_symlink() and digest(path) == checksum

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def respond(self, data, body, content_type='application/octet-stream'):
            try:
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(data.stat().st_size if isinstance(data, Path) else len(data)))
                self.end_headers()
                if body:
                    if isinstance(data, Path):
                        with data.open('rb') as stream:
                            shutil.copyfileobj(stream, self.wfile)
                    else:
                        self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # uv may abandon an in-flight metadata request during resolution.

        def serve(self, body):
            try:
                self.route(body)
            except Exception as error:
                failures.append((self.path, error))
                try:
                    self.send_error(502, 'Wheelhouse adapter failed')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def route(self, body):
            kind, key, *rest = urlsplit(self.path).path.strip('/').split('/')
            if kind in ('cached', 'all'):
                links = []
                for token, attrs in page(canonicalize_name(key)):
                    filename, checksum, _, metadata = artifacts[token]
                    if kind == 'cached':
                        # Filter the initial cache view, not the resolver's candidate ordering.
                        if filename not in initial or not cached(filename, checksum) or not tags.intersection(parse_wheel_filename(filename)[3]):
                            continue
                        if attrs.get('data-requires-python') and not SpecifierSet(attrs['data-requires-python']).contains(python_version(), prereleases=True):
                            continue
                    flags = ''.join(f' {name}="{html.escape(attrs[name] or "", quote=True)}"'
                                    for name in ('data-requires-python', 'data-yanked') if name in attrs)
                    if metadata:
                        # Emit both spellings for Simple API clients; the publisher value is unchanged.
                        flags += ''.join(f' {name}="{html.escape(metadata, quote=True)}"'
                                         for name in ('data-core-metadata', 'data-dist-info-metadata'))
                    links.append(f'<a href="/wheel/{token}/{quote(filename)}#sha256={checksum}"{flags}>{html.escape(filename)}</a>')
                data = ('<!doctype html><html><body>' + '\n'.join(links) + '</body></html>').encode()
                self.respond(data, body, 'text/html')
                return
            filename, checksum, url, metadata = artifacts[key]
            if kind != 'wheel' or len(rest) != 1:
                raise ValueError(f'Unexpected artifact request: {self.path}')
            requested = unquote(rest[0])
            if requested == filename + '.metadata' and metadata:
                parts = urlsplit(url)
                source = parts._replace(path=parts.path + '.metadata').geturl()
                with opener.open(source, timeout=60) as remote:
                    data = remote.read()
                if metadata != 'true':
                    algorithm, expected = metadata.split('=', 1)
                    if hashlib.new(algorithm, data).hexdigest() != expected:
                        raise ValueError(f'Metadata hash mismatch: {source}')
                self.respond(data, body)
                return
            if requested != filename:
                raise ValueError(f'Unexpected artifact filename: {requested}')
            path = pool / filename
            with writes:
                if not cached(filename, checksum):
                    if path.exists() or path.is_symlink():
                        raise ValueError(f'Conflicting cached file: {path}')
                    with tempfile.NamedTemporaryFile(dir=pool, delete=False) as temp:
                        partial = Path(temp.name)
                        try:
                            with opener.open(url, timeout=120) as remote:
                                shutil.copyfileobj(remote, temp)
                            temp.flush()
                            if digest(partial) != checksum:
                                raise ValueError(f'Hash mismatch: {url}')
                            os.fchmod(temp.fileno(), 0o644)
                            os.fsync(temp.fileno())
                            os.link(partial, path)
                            downloads.append(filename)
                            print(f'DOWNLOAD {filename}', flush=True)
                        finally:
                            partial.unlink(missing_ok=True)
                    cached.cache_clear()
            self.respond(path, body)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with tempfile.TemporaryDirectory() as temporary:
                raw = Path(temporary) / 'pylock.toml'
                process = subprocess.run(['uv', '--no-config', '--no-cache', 'pip', 'compile',
                                          str(requirements), '--python', sys.executable,
                                          '--no-python-downloads', '--only-binary=:all:',
                                          '--index', root + '/cached/', '--default-index', root + '/all/',
                                          '--index-strategy', 'unsafe-first-match', '--allow-insecure-host', '127.0.0.1',
                                          '--no-header', '--quiet', '--output-file', str(raw)])
                if failures:
                    request, error = failures[0]
                    raise RuntimeError(f'Adapter request {request}: {error}') from error
                process.check_returncode()
                document = tomllib.loads(raw.read_text())
                for package in document['packages']:
                    if 'index' in package:
                        package['index'] = indexes.get(package['name'], indexes['default'])
                    for wheel in package['wheels']:
                        alias = urlsplit(wheel['url'])
                        kind, token, name = alias.path.strip('/').split('/')
                        filename, checksum, source, _ = artifacts[token]
                        if f'{alias.scheme}://{alias.netloc}' != root or kind != 'wheel' or unquote(name) != filename:
                            raise ValueError('Unexpected lock artifact alias')
                        if wheel['hashes']['sha256'] != checksum:
                            raise ValueError('Lock hash does not match publisher metadata')
                        wheel['name'] = filename
                        wheel['url'] = source
                portable = tomli_w.dumps(document)
                if root in portable:
                    raise ValueError('Non-portable lock')
                output.write_text(portable)
        finally:
            server.shutdown()
            thread.join()
    print(f'Lock-generation wheel downloads: {len(downloads)}', flush=True)
    return downloads


if __name__ == '__main__':
    requirements, pool, output, sources = map(Path, sys.argv[1:])
    lock(requirements, pool, output, tomllib.loads(sources.read_text()))

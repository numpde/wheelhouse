"""A cache-first view of declared Simple API indexes; uv does the resolving."""
import hashlib
import html
import re
import os
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
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import tomli_w

from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.specifiers import SpecifierSet


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def lock(requirements, pool, output, indexes):
    pool.mkdir(parents=True, exist_ok=True)
    for index in indexes.values():
        parts = urlsplit(index)
        if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname == '127.0.0.1'):
            raise ValueError('Only HTTPS publisher indexes or loopback fixtures are allowed')
    artifacts = {}
    writes = Lock()
    downloads = []
    tags = set(sys_tags())

    @lru_cache(maxsize=None)
    def page(package):
        index = indexes.get(package, indexes['default']).rstrip('/') + '/'
        address = urljoin(index, package + '/')
        try:
            with urlopen(Request(address, headers={'Accept': 'text/html'}), timeout=60) as response:
                links = Links()
                links.feed(response.read().decode('utf-8'))
                base = response.geturl()
        except HTTPError as error:
            if error.code == 404:
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
            if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname == '127.0.0.1'):
                raise ValueError('Artifact must use HTTPS (HTTP is for the loopback fixture only)')
            try:
                parse_wheel_filename(filename)
            except InvalidWheelFilename:
                continue
            checksum = hashes[0]
            url = parts._replace(fragment='').geturl()
            key = hashlib.sha256(url.encode()).hexdigest()
            artifacts[key] = (filename, checksum, url)
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

        def serve(self, body):
            kind, key, *_ = self.path.strip('/').split('/')
            if kind in ('cached', 'all'):
                links = []
                for token, attrs in page(key):
                    filename, checksum, _ = artifacts[token]
                    if kind == 'cached':
                        # Filter the cache view, not the resolver's candidate ordering.
                        if not cached(filename, checksum) or not tags.intersection(parse_wheel_filename(filename)[3]):
                            continue
                        if attrs.get('data-requires-python') and not SpecifierSet(attrs['data-requires-python']).contains(python_version(), prereleases=True):
                            continue
                    flags = ''.join(f' {name}="{html.escape(attrs[name] or "", quote=True)}"'
                                    for name in ('data-requires-python', 'data-yanked') if name in attrs)
                    links.append(f'<a href="/wheel/{token}/{quote(filename)}#sha256={checksum}"{flags}>{html.escape(filename)}</a>')
                data = ('<!doctype html><html><body>' + '\n'.join(links) + '</body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)
                return
            filename, checksum, url = artifacts[key]
            path = pool / filename
            with writes:
                if not cached(filename, checksum):
                    if path.exists() or path.is_symlink():
                        raise ValueError(f'Conflicting cached file: {path}')
                    with tempfile.NamedTemporaryFile(dir=pool, delete=False) as temp:
                        partial = Path(temp.name)
                        try:
                            with urlopen(url, timeout=120) as remote:
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
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            if body:
                with path.open('rb') as stream:
                    shutil.copyfileobj(stream, self.wfile)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with tempfile.TemporaryDirectory() as temporary:
                raw = Path(temporary) / 'pylock.toml'
                subprocess.run(['uv', '--no-config', '--no-cache', 'pip', 'compile',
                                str(requirements), '--python', sys.executable,
                                '--no-python-downloads', '--only-binary=:all:',
                                '--index', root + '/cached/', '--default-index', root + '/all/',
                                '--index-strategy', 'unsafe-first-match', '--allow-insecure-host', '127.0.0.1',
                                '--no-header', '--quiet', '--output-file', str(raw)], check=True)
                document = tomllib.loads(raw.read_text())
                for package in document['packages']:
                    if 'index' in package:
                        package['index'] = indexes.get(package['name'], indexes['default'])
                    for wheel in package['wheels']:
                        alias = wheel['url']
                        token = urlsplit(alias).path.split('/')[2]
                        filename, checksum, source = artifacts[token]
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

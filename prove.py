"""Small synthetic wheels and a counted publisher: exercise the actual resolver."""
import hashlib
import html
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import tomli_w
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from platform import machine
from threading import Thread
from urllib.parse import quote, unquote, urlsplit

from lock import lock


def wheel(directory, name, version, tag, requires=(), sidecar=False):
    filename = f'{name}-{version}-{tag}.whl'
    path = directory / filename
    info = f'{name}-{version}.dist-info'
    metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n' + ''.join(f'Requires-Dist: {r}\n' for r in requires) + '\n'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(name + '/__init__.py', f'__version__ = {version!r}\n')
        z.writestr(info + '/METADATA', metadata)
        z.writestr(info + '/WHEEL', f'Wheel-Version: 1.0\nRoot-Is-Purelib: {str(tag == "py3-none-any").lower()}\nTag: {tag}\n')
        z.writestr(info + '/RECORD', '')
    if sidecar:
        path.with_name(filename + '.metadata').write_bytes(metadata.encode())
    return path


def index_page(origin, index, name, paths):
    page = origin / index / name
    page.mkdir(parents=True, exist_ok=True)
    links = []
    for p in paths:
        metadata = p.with_name(p.name + '.metadata')
        flags = f' data-core-metadata="sha256={hashlib.sha256(metadata.read_bytes()).hexdigest()}"' if metadata.exists() else ''
        links.append(f'<a href="/files/{quote(p.name)}#sha256={hashlib.sha256(p.read_bytes()).hexdigest()}"{flags}>{html.escape(p.name)}</a>')
    page.joinpath('index.html').write_text('\n'.join(links))


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: prove.py /wheelhouse.pyz (or --resolver-only for a partial proof)')
    tool = None if sys.argv[1] == '--resolver-only' else [sys.executable, '-B', sys.argv[1]]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin = root / 'origin'; origin.mkdir()
        files = origin / 'files'; files.mkdir()
        pool = root / 'pool'; pool.mkdir()
        assert sys.platform == 'linux' and sys.version_info[:2] == (3, 13) and machine() == 'x86_64', 'Demo target: Linux CPython 3.13 x86-64'
        cpu = wheel(files, 'kernel', '1.0+cpu', 'cp313-cp313-linux_x86_64', ['helper>=1.0', 'leaf==1.0'])
        abi = wheel(files, 'kernel', '1.0+cpu', 'cp312-cp312-linux_x86_64')
        cuda = wheel(files, 'kernel', '1.0+cu130', 'cp313-cp313-linux_x86_64')
        other_platform = wheel(files, 'kernel', '1.0+cpu', 'cp313-cp313-linux_aarch64')
        helper = wheel(files, 'helper', '1.0', 'py3-none-any')
        preferred = wheel(files, 'helper', '1.5', 'py3-none-any')
        newer = wheel(files, 'helper', '2.0', 'py3-none-any')
        leaf = wheel(files, 'leaf', '1.0', 'py3-none-any')
        dead = wheel(files, 'choice', '1.0', 'py3-none-any', ['leaf==2.0'], sidecar=True)
        fallback = wheel(files, 'choice', '2.0', 'py3-none-any', ['leaf==1.0'], sidecar=True)
        index_page(origin, 'cpu', 'kernel', [cpu, abi, cuda, other_platform])
        index_page(origin, 'simple', 'kernel', [cuda])  # CPU pin cannot resolve from the default publisher.
        index_page(origin, 'simple', 'helper', [helper, preferred, newer])
        index_page(origin, 'simple', 'leaf', [leaf])
        index_page(origin, 'simple', 'choice', [dead, fallback])
        transfers, metadata_requests = [], []
        class Publisher(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(origin), **kw)
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.startswith('/redirect/'):
                    self.send_response(302)
                    self.send_header('Location', 'http://example.invalid/simple/')
                    self.end_headers()
                    return
                if self.path.startswith('/files/'):
                    filename = unquote(urlsplit(self.path).path.rsplit('/', 1)[-1])
                    (metadata_requests if filename.endswith('.metadata') else transfers).append(filename)
                super().do_GET()
        requirements = root / 'requirements.in'
        requirements.write_text('kernel==1.0+cpu\n')
        result = root / 'pylock.toml'
        local = root / 'pylock.local.toml'
        with ThreadingHTTPServer(('127.0.0.1', 0), Publisher) as server:
            t = Thread(target=server.serve_forever, daemon=True); t.start()
            publisher = f'http://127.0.0.1:{server.server_port}'
            index = {'default': publisher + '/simple/', 'kernel': publisher + '/cpu/'}
            try:
                for p in (cpu, abi, cuda, other_platform, helper, preferred, leaf):
                    shutil.copyfile(p, pool / p.name)
                lock(requirements, pool, result, index)
                assert transfers == [], transfers
                print('PASS complete, mixed pool: no wheel downloads', flush=True)
                (pool / leaf.name).unlink()
                lock(requirements, pool, result, index)
                assert transfers == [leaf.name], transfers
                print('PASS partial pool: only the missing wheel downloaded', flush=True)
                (pool / cpu.name).unlink()
                lock(requirements, pool, result, index)
                assert transfers == [leaf.name, cpu.name], transfers
                print('PASS incompatible cached ABI/backend/platform cannot satisfy the CPU pin; explicit source used', flush=True)
                document = tomllib.loads(result.read_text())
                assert {p['name']: p['version'] for p in document['packages']} == {'kernel': '1.0+cpu', 'helper': '1.5', 'leaf': '1.0'}
                print('PASS newest eligible cached helper preferred over newer remote version', flush=True)
                for p in document['packages']:
                    for w in p['wheels']:
                        filename = unquote(urlsplit(w['url']).path.rsplit('/', 1)[-1])
                        assert (pool / filename).is_file(), filename
                        assert hashlib.sha256((pool / filename).read_bytes()).hexdigest() == w['hashes']['sha256']
                        assert w['name'] == filename
                        w['path'] = str(pool / filename)
                        del w['url']
                if tool:
                    cold = root / 'cold'; cold.mkdir()
                    command = tool + ['acquire', '--lock', str(result), '--pool', str(cold)]
                    before = len(transfers)
                    subprocess.run(command, check=True)
                    assert sorted(transfers[before:]) == sorted([cpu.name, preferred.name, leaf.name]), transfers
                    before = len(transfers)
                    subprocess.run(command, check=True)
                    assert len(transfers) == before, transfers
                    check = tool + ['check', '--lock', str(result), '--pool', str(cold),
                                    '--local-lock', str(local), '--local-root', str(cold)]
                    subprocess.run(check, check=True)
                    print('PASS released SDK acquisition from a portable lock and zero repeat transfers', flush=True)
                else:
                    local.write_text(tomli_w.dumps(document))
                print('PASS portable source and hash identities', flush=True)

                # An unsatisfiable cached version must not prevent online fallback.
                # Published metadata should avoid downloading the candidate during resolution.
                shutil.copyfile(dead, pool / dead.name)
                choices = root / 'choices.in'; choices.write_text('choice\n')
                choice_lock = root / 'pylock.choice.toml'
                before = len(transfers)
                lock(choices, pool, choice_lock, index)
                selected = {p['name']: p['version'] for p in tomllib.loads(choice_lock.read_text())['packages']}
                assert selected == {'choice': '2.0', 'leaf': '1.0'}, selected
                assert len(transfers) == before and fallback.name + '.metadata' in metadata_requests, (transfers, metadata_requests)
                print('PASS resolver backtracking to remote version using metadata only', flush=True)
                if tool:
                    subprocess.run(tool + ['acquire', '--lock', str(choice_lock), '--pool', str(pool)], check=True)
                    assert transfers[before:] == [fallback.name], transfers
                    print('PASS acquisition downloads only the selected missing wheel', flush=True)

                # Transport failure must be reported before replacing an existing lock.
                original = result.read_bytes()
                try:
                    lock(requirements, pool, result, {'default': publisher + '/redirect/'})
                except RuntimeError as error:
                    assert 'Expected HTTPS publisher URL' in str(error), error
                else:
                    raise AssertionError('Unexpected success following HTTP redirect')
                assert result.read_bytes() == original
                print('PASS publisher redirect failure reaches command boundary; lock unchanged', flush=True)
            finally:
                server.shutdown(); t.join()
        if tool:
            original = result.read_bytes()
            target = cold / preferred.name
            data = target.read_bytes()
            before = len(transfers)
            for damaged in (None, b'corrupt'):
                if damaged is None:
                    target.unlink()
                else:
                    target.write_bytes(damaged)
                local.unlink(missing_ok=True)
                assert subprocess.run(check).returncode != 0
                assert not local.exists() and result.read_bytes() == original
                assert (target.read_bytes() if target.exists() else None) == damaged
                assert len(transfers) == before
                print('PASS offline check rejects ' + ('missing' if damaged is None else 'hash-conflicting') + ' artifact without modifying inputs', flush=True)
            target.write_bytes(data)
            subprocess.run(check, check=True)
        venv = root / 'venv'
        subprocess.run(['uv', '--no-config', '--no-cache', 'venv', '--python', sys.executable, '--offline', str(venv)], check=True)
        subprocess.run(['uv', '--no-config', '--no-cache', 'pip', 'sync', '--python', str(venv / 'bin/python'), '--offline', '--require-hashes', str(local)], check=True)
        subprocess.run([str(venv / 'bin/python'), '-c', 'import kernel, helper, leaf; assert kernel.__version__ == "1.0+cpu" and helper.__version__ == "1.5"'], check=True)
        print('PASS offline install with publisher stopped', flush=True)
        if not tool:
            print('PARTIAL PROOF ONLY: released SDK acquisition and offline check not exercised', flush=True)


if __name__ == '__main__':
    main()

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
from threading import Thread
from urllib.parse import quote, unquote, urlsplit

from lock import lock


def wheel(directory, name, version, tag, requires=()):
    filename = f'{name}-{version}-{tag}.whl'
    path = directory / filename
    info = f'{name}-{version}.dist-info'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(name + '/__init__.py', f'__version__ = {version!r}\n')
        z.writestr(info + '/METADATA', f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n' + ''.join(f'Requires-Dist: {r}\n' for r in requires) + '\n')
        z.writestr(info + '/WHEEL', f'Wheel-Version: 1.0\nRoot-Is-Purelib: {str(tag == "py3-none-any").lower()}\nTag: {tag}\n')
        z.writestr(info + '/RECORD', '')
    return path


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        origin = root / 'origin'; origin.mkdir()
        files = origin / 'files'; files.mkdir()
        pool = root / 'pool'; pool.mkdir()
        assert sys.platform == 'linux' and sys.version_info[:2] == (3, 13), 'Demo target: Linux CPython 3.13 x86-64'
        cpu = wheel(files, 'kernel', '1.0+cpu', 'cp313-cp313-linux_x86_64', ['helper>=1.0', 'leaf==1.0'])
        abi = wheel(files, 'kernel', '1.0+cpu', 'cp312-cp312-linux_x86_64', ['helper>=1.0', 'leaf==1.0'])
        cuda = wheel(files, 'kernel', '1.0+cu130', 'cp313-cp313-linux_x86_64', ['helper>=1.0', 'leaf==1.0'])
        helper = wheel(files, 'helper', '1.0', 'py3-none-any')
        # A newer compatible remote helper must not displace a locally eligible one.
        newer = wheel(files, 'helper', '2.0', 'py3-none-any')
        leaf = wheel(files, 'leaf', '1.0', 'py3-none-any')
        for name, paths in [('kernel', [cpu, abi, cuda]), ('helper', [helper, newer]), ('leaf', [leaf])]:
            page = origin / 'simple' / name
            page.mkdir(parents=True)
            page.joinpath('index.html').write_text('\n'.join(f'<a href="/files/{quote(p.name)}#sha256={hashlib.sha256(p.read_bytes()).hexdigest()}">{html.escape(p.name)}</a>' for p in paths))
        transfers = []
        class Publisher(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(origin), **kw)
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.startswith('/files/'):
                    transfers.append(unquote(urlsplit(self.path).path.rsplit('/', 1)[-1]))
                super().do_GET()
        requirements = root / 'requirements.in'
        requirements.write_text('kernel==1.0+cpu\n')
        result = root / 'pylock.toml'
        with ThreadingHTTPServer(('127.0.0.1', 0), Publisher) as server:
            t = Thread(target=server.serve_forever, daemon=True); t.start()
            index = {'default': f'http://127.0.0.1:{server.server_port}/simple/'}
            try:
                for p in (cpu, abi, cuda, helper, leaf): shutil.copyfile(p, pool / p.name)
                lock(requirements, pool, result, index)
                assert transfers == [], transfers
                print('PASS complete, mixed pool: no wheel downloads', flush=True)
                (pool / leaf.name).unlink()
                lock(requirements, pool, result, index)
                assert transfers == [leaf.name], transfers
                print('PASS partial pool: exactly the missing wheel downloaded; older eligible cached helper retained', flush=True)
                (pool / cpu.name).unlink()
                lock(requirements, pool, result, index)
                assert transfers == [leaf.name, cpu.name], transfers
                print('PASS incompatible cached ABI/backend cannot satisfy the CPU pin', flush=True)
                document = tomllib.loads(result.read_text())
                assert {p['name']: p['version'] for p in document['packages']} == {'kernel': '1.0+cpu', 'helper': '1.0', 'leaf': '1.0'}
                for p in document['packages']:
                    for w in p['wheels']:
                        filename = unquote(urlsplit(w['url']).path.rsplit('/', 1)[-1])
                        assert (pool / filename).is_file(), filename
                        assert hashlib.sha256((pool / filename).read_bytes()).hexdigest() == w['hashes']['sha256']
                        assert w['name'] == filename
                        w['path'] = str(pool / filename)
                        del w['url']
                local = root / 'pylock.local.toml'
                if len(sys.argv) == 2:
                    # Exercise the released tool, not a new acquisition implementation.
                    tool = [sys.executable, '-B', sys.argv[1]]
                    cold = root / 'cold'; cold.mkdir()
                    before = len(transfers)
                    command = tool + ['acquire', '--lock', str(result), '--pool', str(cold)]
                    subprocess.run(command, check=True)
                    assert sorted(transfers[before:]) == sorted([cpu.name, helper.name, leaf.name]), transfers
                    before = len(transfers)
                    subprocess.run(command, check=True)
                    assert len(transfers) == before, transfers
                    subprocess.run(tool + ['check', '--lock', str(result), '--pool', str(cold),
                                           '--local-lock', str(local), '--local-root', str(cold)], check=True)
                    print('PASS released SDK acquisition from a portable lock and zero repeat transfers', flush=True)
                else:
                    local.write_text(tomli_w.dumps(document))
                print('PASS portable source and hash identities', flush=True)
            finally:
                server.shutdown(); t.join()
        venv = root / 'venv'
        subprocess.run(['uv', '--no-config', '--no-cache', 'venv', '--python', sys.executable, '--offline', str(venv)], check=True)
        subprocess.run(['uv', '--no-config', '--no-cache', 'pip', 'sync', '--python', str(venv / 'bin/python'), '--offline', '--require-hashes', str(local)], check=True)
        subprocess.run([str(venv / 'bin/python'), '-c', 'import kernel, helper, leaf; assert kernel.__version__ == "1.0+cpu" and helper.__version__ == "1.0"'], check=True)
        print('PASS offline install with publisher stopped', flush=True)


if __name__ == '__main__':
    main()

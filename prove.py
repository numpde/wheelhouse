"""Run the wheelhouse contract proof inside the prepared tools container."""

import argparse
import base64
import contextlib
import hashlib
import html
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/opt/wheelhouse")

import wheelhouse
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name


def digest(data):
    return hashlib.sha256(data).hexdigest()


def metadata(name, version, requirements=()):
    headers = ["Metadata-Version: 2.3", f"Name: {name}", f"Version: {version}"]
    headers.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    return ("\n".join(headers) + "\n\n").encode()


@dataclass
class Wheel:
    name: str
    version: str
    filename: str
    data: bytes
    sidecar: bytes | None
    redirect: str | None = None


def make_wheel(name, version, requirements=(), *, tag="py3-none-any", sidecar=True, body=None):
    module = name.replace("-", "_")
    filename = f"{module}-{version}-{tag}.whl"
    info = f"{module}-{version}.dist-info"
    core = metadata(name, version, requirements)
    entries = {
        f"{module}/__init__.py": (body or f"__version__ = {version!r}\n").encode(),
        f"{info}/METADATA": core,
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: wheelhouse-proof\n"
            f"Root-Is-Purelib: {'true' if tag == 'py3-none-any' else 'false'}\nTag: {tag}\n"
        ).encode(),
    }
    records = []
    for path, data in entries.items():
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{path},sha256={checksum},{len(data)}\n")
    records.append(f"{info}/RECORD,,\n")
    entries[f"{info}/RECORD"] = "".join(records).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for path, data in entries.items():
            archive.writestr(zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0)), data)
    return Wheel(name, version, filename, target.getvalue(), core if sidecar else None)


class Publisher:
    def __init__(self):
        self.files = {}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                owner.requests.append((path, self.headers.get("Range")))
                parts = path.strip("/").split("/")
                if len(parts) == 2:
                    publisher, package = parts
                    links = []
                    for (source, _), wheel in owner.files.items():
                        if source != publisher or canonicalize_name(wheel.name) != package:
                            continue
                        url = f"/{source}/files/{urllib.parse.quote(wheel.filename)}#sha256={digest(wheel.data)}"
                        attrs = ""
                        if wheel.sidecar is not None:
                            attrs = f' data-core-metadata="sha256={digest(wheel.sidecar)}"'
                        links.append(f'<a href="{html.escape(url, quote=True)}"{attrs}>{wheel.filename}</a>')
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(("<!doctype html>\n" + "\n".join(links)).encode())
                    return
                if len(parts) == 3 and parts[1] == "files":
                    source, _, filename = parts
                    is_metadata = filename.endswith(".metadata")
                    wheel = owner.files.get((source, filename.removesuffix(".metadata")))
                    if wheel is not None:
                        if is_metadata and wheel.redirect:
                            self.send_response(302)
                            self.send_header("Location", wheel.redirect)
                            self.end_headers()
                            return
                        data = wheel.sidecar if is_metadata else wheel.data
                        if data is not None:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data)
                            return
                self.send_error(404)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def index(self, name="simple"):
        return f"http://127.0.0.1:{self.server.server_port}/{name}"

    def add(self, wheel, source="simple"):
        self.files[source, wheel.filename] = wheel
        return wheel

    def wheel_gets(self):
        return [path.rsplit("/", 1)[-1] for path, _ in self.requests if path.endswith(".whl")]


def invoke(arguments, *, error=(), test_http=True):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        status = wheelhouse.main([str(item) for item in arguments], allow_test_http=test_http)
    detail = output.getvalue()
    if error:
        assert status != 0, f"Expected failure: {arguments}\n{detail}"
        for word in error:
            assert word.lower() in detail.lower(), f"Missing diagnostic {word!r}:\n{detail}"
    else:
        assert status == 0, f"Failed: {arguments}\n{detail}"
    return detail


def project(path, dependencies, index, sources=None):
    text = f"[project]\ndependencies = {json.dumps(dependencies)}\n\n[tool.wheelhouse]\nindex = {json.dumps(index)}\n"
    if sources:
        text += "\n[tool.wheelhouse.sources]\n"
        text += "".join(f"{json.dumps(name)} = {json.dumps(url)}\n" for name, url in sources.items())
    path.write_text(text)


def snapshot(path):
    return {
        str(item.relative_to(path)): digest(item.read_bytes()) if item.is_file() else "directory"
        for item in path.rglob("*")
    }


def versions(lock):
    return {record["name"]: record["version"] for record in json.loads(lock.read_text())["resolution"]["packages"]}


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    pool = root / "wheels"
    pool.mkdir()
    declaration = root / "pyproject.toml"
    lock = root / "wheelhouse.lock"
    native_tag = str(next(sys_tags()))

    with Publisher() as publisher:
        engine = publisher.add(make_wheel(
            "wh-demo-engine", "1.0+cpu", ("wh-helper>=1", "wh-leaf==1"),
            tag=native_tag, sidecar=False,
            body="import wh_helper, wh_leaf\n__version__ = '1.0+cpu'\n",
        ), "cpu")
        wrong_python = publisher.add(make_wheel("wh-demo-engine", "1.0+cpu", tag="cp999-cp999-linux_x86_64"), "cpu")
        wrong_platform = publisher.add(make_wheel("wh-demo-engine", "1.0+cpu", tag="py3-none-win_amd64"), "cpu")
        cuda = publisher.add(make_wheel("wh-demo-engine", "9.0+cuda", tag=native_tag))
        helper1 = publisher.add(make_wheel("wh-helper", "1.0"))
        helper15 = publisher.add(make_wheel("wh-helper", "1.5"))
        publisher.add(make_wheel("wh-helper", "2.0"))
        leaf = publisher.add(make_wheel("wh-leaf", "1.0"))
        for wheel in (engine, wrong_python, wrong_platform, cuda, helper1, helper15, leaf):
            (pool / wheel.filename).write_bytes(wheel.data)
        project(declaration, ["wh-demo-engine>=1"], publisher.index(), {engine.name: publisher.index("cpu")})

        before = snapshot(pool)
        overrides = {key: os.environ.get(key) for key in ("UV_DEFAULT_INDEX", "UV_INDEX", "UV_NO_INDEX")}
        try:
            os.environ.update(UV_DEFAULT_INDEX="http://example.invalid/simple", UV_INDEX="http://example.invalid/simple", UV_NO_INDEX="1")
            invoke(["lock", "--project", declaration, "--pool", pool, "--lock", lock])
        finally:
            for key, value in overrides.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        assert versions(lock) == {"wh-demo-engine": "1.0+cpu", "wh-helper": "1.5", "wh-leaf": "1.0"}
        invoke(["acquire", "--lock", lock, "--pool", pool])
        assert snapshot(pool) == before
        assert publisher.wheel_gets() == []
        assert not any(path.startswith("/simple/wh-demo-engine/") for path, _ in publisher.requests)
        print("PASS complete pool: cached versions, source routing, compatible wheels, no wheel downloads", flush=True)

        (pool / leaf.filename).unlink()
        before = snapshot(pool)
        invoke(["lock", "--project", declaration, "--pool", pool, "--lock", lock])
        assert versions(lock)["wh-helper"] == "1.5"
        assert snapshot(pool) == before and publisher.wheel_gets() == []
        invoke(["acquire", "--lock", lock, "--pool", pool])
        assert publisher.wheel_gets() == [leaf.filename]
        before = snapshot(pool)
        invoke(["acquire", "--lock", lock, "--pool", pool])
        assert snapshot(pool) == before and publisher.wheel_gets() == [leaf.filename]
        print("PASS missing dependency: lock transfers zero wheels; acquisition transfers exactly one", flush=True)

        other_project = root / "other.toml"
        other_lock = root / "other.lock"
        choice1 = publisher.add(make_wheel("wh-choice", "1.0", ("wh-leaf==2",), sidecar=False))
        choice2 = publisher.add(make_wheel("wh-choice", "2.0", ("wh-leaf==1",)))
        (pool / choice1.filename).write_bytes(choice1.data)
        project(other_project, ["wh-choice>=1", "wh-leaf==1"], publisher.index())
        before = snapshot(pool)
        gets = publisher.wheel_gets()
        invoke(["lock", "--project", other_project, "--pool", pool, "--lock", other_lock])
        assert versions(other_lock)["wh-choice"] == "2.0"
        assert snapshot(pool) == before and publisher.wheel_gets() == gets
        invoke(["acquire", "--lock", other_lock, "--pool", pool])
        assert publisher.wheel_gets() == gets + [choice2.filename]
        print("PASS backtracking: reject incompatible cached version without fetching wheel bytes", flush=True)

        missing = publisher.add(make_wheel("wh-no-metadata", "1.0", sidecar=False))
        invalid = publisher.add(make_wheel("wh-invalid-metadata", "1.0"))
        invalid.sidecar = b"Metadata-Version: 2.3\nName: wh-invalid-metadata\nVersion: invalid\n\n"
        duplicate = publisher.add(make_wheel("wh-duplicate-python", "1.0"))
        duplicate.sidecar = b"Metadata-Version: 2.3\nName: wh-duplicate-python\nVersion: 1.0\nRequires-Python: >=3\nRequires-Python: <3\n\n"
        direct = publisher.add(make_wheel("wh-direct-reference", "1.0", ("unsafe @ https://example.invalid/unsafe.whl",)))
        redirect = publisher.add(make_wheel("wh-redirect", "1.0"))
        redirect.redirect = "http://example.invalid/metadata"
        wheel_redirect = publisher.add(make_wheel("wh-wheel-redirect", "1.0"))
        wheel_redirect.redirect = f"{publisher.index()}/files/{wheel_redirect.filename}"
        for wheel, diagnostic in (
            (missing, "metadata"), (invalid, "metadata"), (duplicate, "metadata"), (direct, "direct"),
            (redirect, "https"), (wheel_redirect, "wheel"),
        ):
            project(other_project, [f"{wheel.name}=={wheel.version}"], publisher.index())
            other_lock.write_bytes(b"previous lock must survive\n")
            before = snapshot(pool)
            gets = publisher.wheel_gets()
            invoke(["lock", "--project", other_project, "--pool", pool, "--lock", other_lock], error=(diagnostic, wheel.filename))
            assert other_lock.read_bytes() == b"previous lock must survive\n"
            assert snapshot(pool) == before and publisher.wheel_gets() == gets
        assert not any(byte_range for _, byte_range in publisher.requests)
        print("PASS metadata/source failures: clear errors, no wheel/range fallback, previous lock preserved", flush=True)

        bad = publisher.add(make_wheel("wh-metadata-conflict", "1.0", ("wh-unexpected==1",)))
        bad.sidecar = metadata(bad.name, bad.version)
        project(other_project, [f"{bad.name}=={bad.version}"], publisher.index())
        invoke(["lock", "--project", other_project, "--pool", pool, "--lock", other_lock])
        locked = other_lock.read_bytes()
        before = snapshot(pool)
        invoke(["acquire", "--lock", other_lock, "--pool", pool], error=("metadata", bad.filename))
        assert snapshot(pool) == before and other_lock.read_bytes() == locked
        assert not (pool / bad.filename).exists()
        (pool / bad.filename).write_bytes(bad.data)
        before = snapshot(pool)
        local = root / "pylock.failed.toml"
        for action in ("acquire", "check"):
            invoke([action, "--lock", other_lock, "--pool", pool, "--local-lock", local], error=("metadata", bad.filename))
            assert snapshot(pool) == before and other_lock.read_bytes() == locked and not local.exists()
        evidence = json.loads(locked)
        evidence["metadata"].pop(digest(bad.data))
        other_lock.write_text(json.dumps(evidence))
        invoke(["check", "--lock", other_lock, "--pool", pool, "--local-lock", local], error=("metadata",))
        assert snapshot(pool) == before and not local.exists()
        print("PASS metadata agreement: downloaded/cached mismatches and absent evidence are rejected", flush=True)

        original = lock.read_bytes()
        saved_leaf = (pool / leaf.filename).read_bytes()
        for contents in (None, b"incorrect wheel bytes\n"):
            if contents is None:
                (pool / leaf.filename).unlink()
            else:
                (pool / leaf.filename).write_bytes(contents)
            before = snapshot(pool)
            gets = publisher.wheel_gets()
            invoke(["check", "--lock", lock, "--pool", pool, "--local-lock", local], error=(leaf.filename,))
            if contents is not None:
                invoke(["acquire", "--lock", lock, "--pool", pool], error=(leaf.filename,))
            assert snapshot(pool) == before and lock.read_bytes() == original and not local.exists()
            assert publisher.wheel_gets() == gets
            (pool / leaf.filename).write_bytes(saved_leaf)
        print("PASS missing/conflicting files: checking preserves inputs and never acquires", flush=True)

    print("PASS prepared lock and pool; publisher stopped before the fresh-container proof", flush=True)


def prepare_real(root):
    root = root / "real"
    root.mkdir(parents=True)
    pool = root / "wheels"
    pool.mkdir()
    declaration = root / "pyproject.toml"
    lock = root / "wheelhouse.lock"
    project(declaration, ["requests==2.32.3", "numpy==2.2.2"], "https://pypi.org/simple")
    invoke(["lock", "--project", declaration, "--pool", pool, "--lock", lock], test_http=False)
    assert snapshot(pool) == {}
    invoke(["acquire", "--lock", lock, "--pool", pool], test_http=False)
    before = snapshot(pool)
    invoke(["lock", "--project", declaration, "--pool", pool, "--lock", root / "warm.lock"], test_http=False)
    cold = {tuple(artifact[:3]) for artifact in wheelhouse.load_locked(lock)[2]}
    warm = {tuple(artifact[:3]) for artifact in wheelhouse.load_locked(root / "warm.lock")[2]}
    assert cold == warm
    invoke(["acquire", "--lock", lock, "--pool", pool], test_http=False)
    assert snapshot(pool) == before
    print("PASS real PyPI: resolve without wheels, acquire Requests/NumPy closure, reuse complete pool", flush=True)


def offline(root, real=False):
    if real:
        root = root / "real"
    pool = root / "wheels"
    lock = root / "wheelhouse.lock"
    before = snapshot(root)
    with tempfile.TemporaryDirectory(prefix="wheelhouse-offline-") as temporary:
        temporary = Path(temporary)
        projection = temporary / "pylock.toml"
        venv = temporary / "venv"
        invoke(["check", "--lock", lock, "--pool", pool, "--local-lock", projection], test_http=not real)
        environment = {name: value for name, value in os.environ.items() if not name.startswith("UV_")}
        subprocess.run(["uv", "--no-config", "--no-cache", "venv", str(venv), "--python", sys.executable, "--no-python-downloads"], check=True, env=environment)
        subprocess.run([
            "uv", "--no-config", "--no-cache", "pip", "sync", "--offline", "--no-index",
            "--no-python-downloads", "--only-binary=:all:", "--require-hashes", "--python", str(venv / "bin/python"), str(projection),
        ], check=True, env=environment)
        code = (
            "import numpy, requests; assert numpy.__version__ == '2.2.2'; assert requests.__version__ == '2.32.3'"
            if real else
            "import wh_demo_engine, wh_helper, wh_leaf; assert wh_demo_engine.__version__ == '1.0+cpu'; assert wh_helper.__version__ == '1.5'; assert wh_leaf.__version__ == '1.0'"
        )
        subprocess.run([str(venv / "bin/python"), "-I", "-c", code], check=True, env=environment)
    assert snapshot(root) == before
    print(f"PASS fresh offline container: {'Requests/NumPy' if real else 'synthetic closure'} installed; inputs unchanged", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "offline", "prepare-real", "offline-real"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.root)
    elif args.mode == "prepare-real":
        prepare_real(args.root)
    else:
        offline(args.root, real=args.mode == "offline-real")

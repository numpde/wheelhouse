"""Strict metadata-only locking and verified consumption of a shared wheel pool."""

import argparse
import html
import ipaddress
import platform
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from contextlib import contextmanager
from functools import lru_cache
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename
from packaging.version import Version


def validate_url(url, allow_test_http=False, previous=None):
    if not isinstance(url, str) or any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError(f"Expected an HTTPS URL: {url!r}")
    parts = urlsplit(url)
    loopback = False
    if allow_test_http and parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
    if not parts.hostname or parts.username is not None or parts.password is not None or not (parts.scheme == "https" or loopback):
        raise ValueError(f"Expected an HTTPS URL without credentials: {url}")
    if previous is not None and urlsplit(previous).scheme == "https" and parts.scheme != "https":
        raise ValueError(f"HTTPS downgrade forbidden: {previous} -> {url}")
    _ = parts.port  # Reject malformed ports before opening a request.


@contextmanager
def network_policy(allow_test_http=False, metadata_only=False):
    def permitted(url, previous=None):
        validate_url(url, allow_test_http, previous)
        if metadata_only and unquote(urlsplit(url).path).lower().endswith(".whl"):
            raise RuntimeError(f"Wheel-byte request forbidden during resolution: {url}")

    class HTTPSRequests(urllib.request.BaseHandler):
        def http_request(self, request):
            permitted(request.full_url)
            return request

        https_request = http_request

    class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, stream, code, message, headers, new_url):
            try:
                permitted(new_url, previous=request.full_url)
            except ValueError as error:
                # Keep the reason visible through the released SDK's error handling.
                raise RuntimeError(str(error)) from error
            return super().redirect_request(request, stream, code, message, headers, new_url)

    previous = urllib.request._opener
    urllib.request.install_opener(urllib.request.build_opener(HTTPSRequests(), HTTPSRedirects()))
    try:
        yield
    finally:
        urllib.request.install_opener(previous)


def atomic_write(path, content):
    path = Path(path)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


import hashlib
import io
import json
import os
import re
import stat
import sysconfig
import tempfile
import token
import tokenize
import zipfile
from pathlib import Path

from packaging.markers import default_environment
from packaging.metadata import Metadata
from packaging.pylock import Pylock
from packaging.utils import canonicalize_name, parse_wheel_filename
from wheelhouse_tool import __main__ as sdk
from wheelhouse_tool.validation.upstream.pylock import require_pylock_filename


def target_environment(extra=()):
    """Describe the running target, recording kernel details only if observed."""
    environment = default_environment()
    optional = {"platform_release", "platform_version"}
    extra = set(extra)
    if not extra <= set(environment):
        raise ValueError(f"Unknown target marker fields: {sorted(extra - set(environment))}")
    result = {name: value for name, value in environment.items() if name not in optional or name in extra}
    abi = sysconfig.get_config_var("SOABI")
    if not isinstance(abi, str) or not abi:
        raise ValueError("The running Python does not provide a target ABI")
    return {**result, "abi": abi}


def marker_fields(marker):
    """Inspect the public marker representation without treating literals as variables."""
    if marker is None:
        return set()
    names = set(default_environment())
    return {
        item.string
        for item in tokenize.generate_tokens(io.StringIO(str(marker)).readline)
        if item.type == token.NAME and item.string in names
    }


def validate_metadata(content, filename):
    """Validate exact wheel metadata and report the target fields it consults."""
    try:
        metadata = Metadata.from_email(content, validate=True)
    except ExceptionGroup as error:
        raise ValueError(f"{filename}: invalid METADATA: {error.exceptions!r}") from error
    name, version, _build, _tags = parse_wheel_filename(filename)
    if canonicalize_name(metadata.name) != name or metadata.version != version:
        raise ValueError(f"{filename}: METADATA name/version does not match the wheel")
    fields = set()
    for requirement in metadata.requires_dist or ():
        if requirement.url is not None:
            raise ValueError(f"{filename}: direct dependency URLs are unsupported: {requirement}")
        fields.update(marker_fields(requirement.marker))
    return fields


def wheel_metadata(path, directory_fd=None):
    """Read one canonical METADATA entry without extracting or executing the wheel."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path}: expected a regular wheel file")
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = [entry for entry in archive.infolist() if entry.filename.endswith(".dist-info/METADATA")]
                if len(entries) != 1 or len(entries[0].filename.split("/")) != 2:
                    raise ValueError(f"{path}: expected exactly one root .dist-info/METADATA")
                entry = entries[0]
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError(f"{path}: METADATA must not be a symlink")
                return archive.read(entry)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            raise ValueError(f"{path}: cannot read wheel METADATA: {error}") from error


def _sha256_text(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_locked(lock, allow_test_http=False):
    """Validate mandatory evidence for every permitted wheel before selection."""
    lock = Path(lock)
    if lock.suffix != ".lock":
        raise ValueError("The authoritative wheelhouse envelope must have a .lock filename")
    envelope = json.loads(lock.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or set(envelope) != {"wheelhouse-lock", "target", "resolution", "metadata"}:
        raise ValueError("Expected a wheelhouse lock with target, resolution and mandatory metadata evidence")
    if type(envelope["wheelhouse-lock"]) is not int or envelope["wheelhouse-lock"] != 1:
        raise ValueError("Unsupported wheelhouse lock version")
    target = envelope["target"]
    if not isinstance(target, dict):
        raise ValueError("The lock target must be an object")
    expected_target = target_environment(set(target) - {"abi"})
    if target != expected_target:
        differing = sorted(set(target) ^ set(expected_target) | {key for key in target.keys() & expected_target.keys() if target[key] != expected_target[key]})
        raise ValueError(f"The lock was created for a different target; differing fields: {differing}")
    if not isinstance(envelope["resolution"], dict):
        raise ValueError("The lock resolution must be a standard pylock object")
    evidence = envelope["metadata"]
    if not isinstance(evidence, dict) or any(not _sha256_text(key) or not _sha256_text(value) for key, value in evidence.items()):
        raise ValueError("Metadata evidence must map wheel SHA-256 values to METADATA SHA-256 values")
    document = Pylock.from_dict(envelope["resolution"])
    covered = set()
    for package in document.packages:
        if package.version is None or not package.wheels or any(value is not None for value in (package.sdist, package.archive, package.vcs, package.directory)):
            raise ValueError(f"{package.name}: the lock must contain versioned registry wheels only")
        validate_url(package.index, allow_test_http=allow_test_http)
        for wheel in package.wheels:
            if wheel.path is not None:
                raise ValueError(f"{package.name}: authoritative locks must not contain local wheel paths")
            validate_url(wheel.url, allow_test_http=allow_test_http)
            name = wheel.filename
            if "/" in name or "\\" in name or any(ord(character) < 32 or ord(character) == 127 for character in name):
                raise ValueError(f"{package.name}: unsafe wheel filename {name!r}")
            digest = wheel.hashes.get("sha256")
            if not _sha256_text(digest) or digest not in evidence:
                raise ValueError(f"{name} from {wheel.url}: required artifact hash or metadata evidence is absent")
            covered.add(digest)
    if set(evidence) != covered:
        raise ValueError("Metadata evidence contains entries not identified by a locked wheel")
    artifacts = sdk.selected(lock, (), document=document)
    return envelope, document, artifacts


def _check_metadata(pool, artifact, envelope, directory_fd):
    name, digest, url, _package, _wheel = artifact
    content = wheel_metadata(name, directory_fd=directory_fd)
    actual = hashlib.sha256(content).hexdigest()
    expected = envelope["metadata"][digest]
    if actual != expected:
        raise ValueError(f"{name} from {url}: METADATA SHA-256 {actual} differs from resolution evidence {expected}")
    missing_fields = validate_metadata(content, name) - set(envelope["target"])
    if missing_fields:
        raise ValueError(f"{name} from {url}: lock target omits metadata marker fields {sorted(missing_fields)}")


def _check_cached(pool, artifacts, envelope, directory_fd):
    missing = []
    for artifact in artifacts:
        name, digest, url, _package, _wheel = artifact
        try:
            if not sdk.inspect_artifact(pool, name, digest, directory_fd):
                missing.append(artifact)
                continue
            _check_metadata(pool, artifact, envelope, directory_fd)
        except (OSError, ValueError) as error:
            raise ValueError(f"{name} from {url}: {error}") from error
    return missing


def consume(action, lock, pool, local_output=None, allow_test_http=False):
    """Consume the existing selection, publishing only completely verified wheels."""
    if action not in {"acquire", "check"}:
        raise ValueError(f"Unknown lock consumption operation: {action}")
    lock, pool = Path(lock), Path(pool)
    if local_output is not None:
        local_output = Path(local_output)
        require_pylock_filename(local_output.name)
        if local_output.resolve() == lock.resolve():
            raise ValueError("A disposable local projection must not replace the authoritative lock")
        if pool.resolve() in local_output.resolve().parents:
            raise ValueError("Write the disposable local projection outside the shared wheel pool")
    envelope, document, artifacts = load_locked(lock, allow_test_http)
    downloaded = []
    with sdk.pool_lock(pool, exclusive=False) as directory_fd:
        missing = _check_cached(pool, artifacts, envelope, directory_fd)
    if missing and action == "check":
        raise FileNotFoundError(f"Missing locked wheels: {', '.join(artifact[0] for artifact in missing)}")
    if missing:
        with sdk.acquisition_lock(pool) as directory_fd:
            # Another cooperating writer may have completed while we waited.
            missing = _check_cached(pool, artifacts, envelope, directory_fd)
            if missing:
                with tempfile.TemporaryDirectory(prefix=".wheelhouse-verify-", dir=pool) as temporary:
                    staging = Path(temporary)
                    with sdk.pool_lock(staging, exclusive=False) as staging_fd, network_policy(allow_test_http):
                        for artifact in missing:
                            name, digest, url, _package, _wheel = artifact
                            sdk.download(staging, name, digest, url, staging_fd)
                            _check_metadata(staging, artifact, envelope, staging_fd)
                            try:
                                os.link(name, name, src_dir_fd=staging_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                            except FileExistsError:
                                if _check_cached(pool, [artifact], envelope, directory_fd):
                                    raise FileNotFoundError(f"{name}: a competing published wheel disappeared")
                            os.fsync(directory_fd)
                            downloaded.append(name)
                            os.unlink(name, dir_fd=staging_fd)
    if local_output is not None:
        projection = sdk.local_lock(lock, pool.resolve(), document=document, artifacts=artifacts)
        atomic_write(local_output, projection.encode("utf-8"))
    return {"artifacts": [artifact[0] for artifact in artifacts], "downloaded": downloaded}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def read_project(path, allow_test_http=False):
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    project = data.get("project", {})
    if "dependencies" in project.get("dynamic", []):
        raise ValueError("Dynamic dependencies require a build; supply static project.dependencies")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not all(isinstance(x, str) for x in dependencies):
        raise ValueError("Expected a project.dependencies list of named PEP 508 requirements")
    fields = set()
    requirements = []
    for text in dependencies:
        requirement = Requirement(text)
        if requirement.url is not None or "\n" in text or "\r" in text:
            raise ValueError(f"Direct URLs and requirements-file directives are unsupported: {text}")
        fields.update(marker_fields(requirement.marker))
        requirements.append(str(requirement))
    if project.get("requires-python") and not SpecifierSet(project["requires-python"]).contains(platform.python_version(), prereleases=True):
        raise ValueError(f"This interpreter does not satisfy project.requires-python: {project['requires-python']}")
    settings = data.get("tool", {}).get("wheelhouse", {})
    if "index" not in settings:
        raise ValueError("Declare tool.wheelhouse.index in the project file")
    indexes = {"default": settings["index"]}
    for name, source in settings.get("sources", {}).items():
        normalized = canonicalize_name(name, validate=True)
        if normalized in indexes:
            raise ValueError(f"Duplicate or reserved package source: {name}")
        indexes[normalized] = source
    for source in indexes.values():
        validate_url(source, allow_test_http)
        if urlsplit(source).query or urlsplit(source).fragment:
            raise ValueError(f"Index URLs must be plain base URLs: {source}")
    return requirements, indexes, fields


def lock_project(project, pool, output, allow_test_http=False):
    """Give uv metadata and two views of each declared publisher; never wheel bytes."""
    project, pool, output = map(Path, (project, pool, output))
    requirements, indexes, marker_names = read_project(project, allow_test_http)
    if output.suffix != ".lock":
        raise ValueError("Use a .lock filename for the wheelhouse envelope, not pylock.toml")
    if not pool.is_dir():
        raise ValueError(f"Shared wheelhouse must already exist: {pool}")
    if output.resolve().is_relative_to(pool.resolve()):
        raise ValueError("Write the project lock outside the shared wheelhouse")
    initial = {path.name for path in pool.iterdir()}
    tags = set(sys_tags())
    artifacts, observed, failures = {}, {}, []
    gate = threading.RLock()

    @lru_cache(maxsize=None)
    def cached(name, checksum):
        return name in initial and checksum is not None and sdk.inspect_artifact(pool, name, checksum)

    @lru_cache(maxsize=None)
    def page(package):
        index = indexes.get(package, indexes["default"])
        address = index.rstrip("/") + "/" + package + "/"
        try:
            with urllib.request.urlopen(urllib.request.Request(address, headers={"Accept": "text/html"}), timeout=30) as response:
                parser = Links()
                parser.feed(response.read().decode("utf-8"))
                base = response.geturl()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                return []
            raise RuntimeError(f"Publisher index {address}: {error}") from error
        except Exception as error:
            raise RuntimeError(f"Publisher index {address}: {error}") from error
        entries = []
        for attrs in parser.links:
            source = urljoin(base, attrs.get("href") or "")
            parts = urlsplit(source)
            filename = unquote(parts.path.rsplit("/", 1)[-1])
            if not filename.endswith(".whl"):
                continue
            try:
                name, version, _, wheel_tags = parse_wheel_filename(filename)
            except InvalidWheelFilename:
                continue
            if "/" in filename or "\\" in filename or name != package:
                raise ValueError(f"Wrong artifact identity from {address}: {filename}")
            validate_url(source, allow_test_http)
            values = parse_qs(parts.fragment).get("sha256", [])
            checksum = values[0] if len(values) == 1 and re.fullmatch(r"[0-9a-f]{64}", values[0]) else None
            url = parts._replace(fragment="").geturl()
            token = hashlib.sha256(f"{url}\n{checksum}".encode()).hexdigest()
            artifact = {"name": filename, "sha256": checksum, "url": url, "index": index,
                        "package": name, "version": version, "tags": wheel_tags,
                        "metadata": attrs.get("data-core-metadata", attrs.get("data-dist-info-metadata"))}
            if token in artifacts and artifacts[token] != artifact:
                raise ValueError(f"Conflicting index entries for {filename} at {index}")
            artifacts[token] = artifact
            entries.append((token, attrs))
        return entries

    @lru_cache(maxsize=None)
    def metadata(token):
        item = artifacts[token]
        filename, checksum = item["name"], item["sha256"]
        try:
            if checksum is None:
                raise ValueError("Publisher did not supply one valid artifact SHA-256")
            if cached(filename, checksum):
                data = wheel_metadata(pool / filename)
            else:
                hint = item["metadata"]
                if not hint:
                    raise ValueError("Required separate metadata is unavailable; wheel downloads are forbidden during resolution")
                parts = urlsplit(item["url"])
                address = parts._replace(path=parts.path + ".metadata").geturl()
                with urllib.request.urlopen(address, timeout=30) as response:
                    data = response.read()
                if hint != "true":
                    algorithm, expected = hint.split("=", 1)
                    if hashlib.new(algorithm, data).hexdigest() != expected:
                        raise ValueError(f"Publisher metadata hash mismatch: {address}")
            marker_names.update(validate_metadata(data, filename))
            return data
        except Exception as error:
            raise RuntimeError(f"Metadata for {filename} from {item['index']}: {error}") from error

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.serve(True)

        def do_HEAD(self):
            self.serve(False)

        def serve(self, body):
            try:
                # Serialize access to the snapshot and evidence, not uv's solving.
                with gate:
                    data, content_type = self.route(body)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # uv can abandon an in-flight prefetch.
            except Exception as error:
                with gate:
                    failures.append(error)
                try:
                    self.send_error(502, "Wheelhouse metadata request failed")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def route(self, body):
            route = urlsplit(self.path).path.strip("/").split("/")
            if len(route) == 2 and route[0] in ("cached", "all"):
                kind, package = route
                package = canonicalize_name(package, validate=True)
                links = []
                for token, attrs in page(package):
                    item = artifacts[token]
                    filename, checksum = item["name"], item["sha256"]
                    if kind == "cached":
                        if not item["tags"].intersection(tags):
                            continue
                        if attrs.get("data-requires-python") and not SpecifierSet(attrs["data-requires-python"]).contains(platform.python_version(), prereleases=True):
                            continue
                        try:
                            if not cached(filename, checksum):
                                continue
                        except Exception as error:
                            raise RuntimeError(f"Local wheel {filename} from {item['index']}: {error}") from error
                    flags = ''.join(f' {name}="{html.escape(attrs[name] or "", quote=True)}"'
                                    for name in ("data-requires-python", "data-yanked") if name in attrs)
                    # Always force metadata requests, including explicit errors for missing sidecars.
                    flags += ' data-core-metadata="true" data-dist-info-metadata="true"'
                    fragment = f"#sha256={checksum}" if checksum else ""
                    links.append(f'<a href="/wheel/{token}/{quote(filename)}{fragment}"{flags}>{html.escape(filename)}</a>')
                return ("<!doctype html><html><body>" + "\n".join(links) + "</body></html>").encode(), "text/html"
            if len(route) != 3 or route[0] != "wheel" or route[1] not in artifacts:
                raise ValueError(f"Unexpected resolver request: {self.path}")
            _, token, filename = route
            item = artifacts[token]
            if unquote(filename) != item["name"] + ".metadata":
                raise RuntimeError(f"Wheel-byte request forbidden during resolution: {item['name']} from {item['index']}")
            data = metadata(token)
            if body:
                identity = (item["index"], item["package"], item["version"])
                observed.setdefault(identity, set()).add(hashlib.sha256(data).hexdigest())
            return data, "application/octet-stream"

    with tempfile.TemporaryDirectory(prefix="wheelhouse-resolve-") as temporary, network_policy(allow_test_http, metadata_only=True):
        work = Path(temporary)
        sanitized = work / "requirements.in"
        sanitized.write_text("\n".join(requirements) + "\n")
        raw = work / "pylock.toml"
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            root = f"http://127.0.0.1:{server.server_port}"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("UV_", "PIP_")) and key != "VIRTUAL_ENV"}
            environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,::1"
            try:
                process = subprocess.run([
                    "uv", "--no-config", "--no-cache", "pip", "compile", str(sanitized),
                    "--python", sys.executable, "--no-python-downloads", "--only-binary=:all:",
                    "--index", root + "/cached/", "--default-index", root + "/all/",
                    "--index-strategy", "unsafe-first-match", "--allow-insecure-host", "127.0.0.1",
                    "--no-header", "--quiet", "--output-file", str(raw),
                ], cwd=work, env=environment, capture_output=True, text=True)
            finally:
                server.shutdown()
                thread.join()
        if failures:
            raise RuntimeError(str(failures[0])) from failures[0]
        if process.returncode:
            raise RuntimeError(f"uv could not resolve dependencies:\n{process.stderr.strip()}")
        resolution = tomllib.loads(raw.read_text())
        evidence = {}
        for package in resolution.get("packages", []):
            name = canonicalize_name(package["name"])
            index = indexes.get(name, indexes["default"])
            identity = (index, name, Version(package["version"]))
            digests = observed.get(identity, set())
            if len(digests) != 1:
                raise ValueError(f"Expected one consistent resolver metadata digest for {name} {package['version']} from {index}; got {sorted(digests)}")
            expected = next(iter(digests))
            package["index"] = index
            if not package.get("wheels") or any(key in package for key in ("sdist", "archive", "vcs", "directory")):
                raise ValueError(f"Expected registry wheels only for {name}")
            for wheel in package["wheels"]:
                alias = urlsplit(wheel["url"])
                parts = alias.path.strip("/").split("/")
                if f"{alias.scheme}://{alias.netloc}" != root or len(parts) != 3 or parts[0] != "wheel":
                    raise ValueError(f"Unexpected artifact URL from uv for {name}")
                item = artifacts[parts[1]]
                if unquote(parts[2]) != item["name"] or (item["index"], item["package"], item["version"]) != identity:
                    raise ValueError(f"Artifact identity changed during resolution for {name}")
                checksum = item["sha256"]
                if wheel["hashes"].get("sha256") != checksum or checksum is None:
                    raise ValueError(f"uv artifact hash disagrees with publisher for {item['name']}")
                wheel["url"] = item["url"]
                wheel["name"] = item["name"]
                # Upload timestamps are optional provenance, not artifact identity;
                # omit TOML datetime objects from the JSON envelope.
                wheel.pop("upload-time", None)
                if checksum in evidence and evidence[checksum] != expected:
                    raise ValueError(f"One wheel was assigned inconsistent metadata: {item['name']}")
                # uv may use one wheel's metadata for a version. Every retained wheel
                # must match those exact bytes before acquisition or offline use.
                evidence[checksum] = expected
        envelope = {"wheelhouse-lock": 1, "target": target_environment(marker_names),
                    "resolution": resolution, "metadata": evidence}
        encoded = (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode()
        if root.encode() in encoded:
            raise ValueError("Temporary resolver URLs remain in the lock")
        staged = work / "wheelhouse.lock"
        staged.write_bytes(encoded)
        load_locked(staged, allow_test_http)  # Check the exact consumer contract before replacing the lock.
        atomic_write(output, encoded)
    return {"lock": str(output), "packages": len(resolution.get("packages", [])), "wheel-downloads": 0}


def main(argv=None, *, allow_test_http=False):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    for action in ("lock", "acquire", "check"):
        command = subcommands.add_parser(action)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--pool", type=Path, required=True)
        if action == "lock":
            command.add_argument("--project", type=Path, required=True)
        else:
            command.add_argument("--local-lock", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.action == "lock":
            result = lock_project(arguments.project, arguments.pool, arguments.lock,
                                  allow_test_http=allow_test_http)
        else:
            result = consume(arguments.action, arguments.lock, arguments.pool,
                             arguments.local_lock, allow_test_http=allow_test_http)
    except Exception as error:
        print(f"wheelhouse: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

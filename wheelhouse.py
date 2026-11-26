"""Strict metadata-only locking and verified consumption of a shared wheel pool."""

import argparse
import fcntl
import hashlib
import html
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import token
import tokenize
import tomllib
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import tomli_w
from packaging.markers import default_environment
from packaging.metadata import Metadata, parse_email
from packaging.pylock import Pylock, is_valid_pylock_path
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename


def validate_url(url):
    parts = urlsplit(url)

    if (parts.scheme != "https") or (not parts.hostname) or (parts.username is not None):
        raise ValueError(f"Expected an HTTPS URL without credentials: {url=!s}")

    return parts


@contextmanager
def network_policy(metadata_only=False):
    def permitted(url):
        parts = validate_url(url)

        if metadata_only and unquote(parts.path).lower().endswith(".whl"):
            raise RuntimeError(f"Wheel-byte request forbidden during resolution: {url=!s}")

    class HTTPSRequests(urllib.request.BaseHandler):
        def http_request(self, request):
            permitted(request.full_url)

            return request

        https_request = http_request

    class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, stream, code, message, headers, new_url):
            permitted(new_url)

            return super().redirect_request(request, stream, code, message, headers, new_url)

    previous = urllib.request._opener

    urllib.request.install_opener(urllib.request.build_opener(HTTPSRequests(), HTTPSRedirects()))

    try:
        yield
    finally:
        urllib.request.install_opener(previous)


def atomic_write(path, content):
    with tempfile.TemporaryDirectory(
        prefix=f".{path.name}.",  # machine-string: TemporaryDirectory requires a filesystem prefix without expression labels.
        dir=path.parent,
    ) as temporary:
        staged = Path(temporary) / path.name

        staged.write_bytes(content)
        staged.replace(path)


def target_environment(extra=()):
    """Describe the running target, recording kernel details only if observed."""
    environment = default_environment()
    optional = {"platform_release", "platform_version"}
    result = {name: value for (name, value) in environment.items() if (name not in optional) or (name in extra)}

    return {**result, "abi": sysconfig.get_config_var("SOABI")}


def marker_fields(marker):
    """Inspect the public marker representation without treating literals as variables."""

    if marker is None:
        return set()

    names = set(default_environment())

    return {
        item.string
        for item in tokenize.generate_tokens(io.StringIO(str(marker)).readline)
        if (item.type == token.NAME) and (item.string in names)
    }


def validate_metadata(content, filename):
    """Validate exact wheel metadata and report the target fields it consults."""

    # Resolution uses these fields. Older wheels can contain unrelated legacy
    # headers (for example License-File) that are not valid under newer schemas.
    # The evidence still hashes every byte, including those unrelated headers.
    fields = {"metadata_version", "name", "version", "requires_dist", "requires_python", "provides_extra"}

    (raw, unparsed) = parse_email(content)
    invalid = set(unparsed).intersection(field.replace("_", "-") for field in fields)

    if invalid:
        invalid_fields = sorted(invalid)

        raise ValueError(f"{filename=!s}: invalid dependency METADATA fields: {invalid_fields=!s}")

    try:
        metadata = Metadata.from_raw({key: value for (key, value) in raw.items() if key in fields}, validate=True)
    except ExceptionGroup as error:
        raise ValueError(f"{filename=!s}: invalid METADATA: {error.exceptions=!r}") from error

    (name, version, _build, _tags) = parse_wheel_filename(filename)

    if (canonicalize_name(metadata.name) != name) or (metadata.version != version):
        raise ValueError(f"{filename=!s}: METADATA name/version does not match the wheel")

    fields = set()

    for requirement in metadata.requires_dist or ():
        if requirement.url is not None:
            raise ValueError(f"{filename=!s}: direct dependency URLs are unsupported: {requirement=!s}")

        fields.update(marker_fields(requirement.marker))

    return fields


@contextmanager
def wheel_file(path, digest=None, directory_fd=None):
    """Open a regular wheel and, when requested, verify its bytes before reading it."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)

    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path=!s}: expected a regular wheel file")

        if digest is not None:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()

            if actual != digest:
                raise ValueError(f"{path=!s}: SHA-256 {actual=!s} differs from locked {digest=!s}")

            stream.seek(0)

        yield stream


def wheel_metadata(stream):
    with zipfile.ZipFile(stream) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]

        if (len(entries) != 1) or (entries[0].count("/") != 1):
            raise ValueError("Expected exactly one root .dist-info/METADATA")

        return archive.read(entries[0])


def _sha256_text(value):
    return isinstance(value, str) and (re.fullmatch(r"[0-9a-f]{64}", value) is not None)


@contextmanager
def pool_lock(pool):
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)

    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def acquisition_lock(pool):
    # Keep this lock file in place so concurrent writers share one lock inode.
    with pool_lock(pool) as directory_fd:
        descriptor = os.open(
            ".wheelhouse-acquire.lock",
            os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
            dir_fd=directory_fd
        )

        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(".wheelhouse-acquire.lock must be a regular file")

            fcntl.flock(stream, fcntl.LOCK_EX)
            yield directory_fd


def verify_wheel(path, wheel, evidence, directory_fd=None):
    """Use the same artifact and metadata checks for cached and downloaded wheels."""
    digest = wheel.hashes["sha256"]

    try:
        with wheel_file(path, digest, directory_fd) as stream:
            if hashlib.sha256(wheel_metadata(stream)).hexdigest() != evidence[digest]:
                raise ValueError("Embedded METADATA differs from resolution metadata")
    except FileNotFoundError:
        raise
    except Exception as error:
        raise ValueError(f"{wheel.filename=!s} from {wheel.url=!s}: {error=!s}") from error


def local_lock(document, pool, artifacts):
    packages = [
        replace(package, wheels=[replace(wheel, url=None, path=str(pool / wheel.filename))])
        for (package, wheel) in artifacts
    ]

    return tomli_w.dumps(replace(document, packages=packages).to_dict())


def registry_wheels(package):
    if (package.version is None) or (not package.wheels) or (package.sdist is not None):
        raise ValueError(f"{package.name=!s}: expected versioned registry wheels only")

    return package.wheels


def load_locked(lock):
    """Validate mandatory evidence for every permitted wheel before selection."""
    envelope = json.loads(lock.read_text(encoding="utf-8"))

    if envelope["wheelhouse-lock"] != 1:
        raise ValueError("Unsupported wheelhouse lock version")

    target = envelope["target"]
    expected_target = target_environment(target)

    if target != expected_target:
        differing = sorted(key for key in target.keys() | expected_target.keys() if target.get(key) != expected_target.get(key))

        raise ValueError(f"The lock was created for a different target; differing fields: {differing=!s}")

    evidence = envelope["metadata"]
    document = Pylock.from_dict(envelope["resolution"])

    for package in document.packages:
        validate_url(package.index)

        for wheel in registry_wheels(package):
            validate_url(wheel.url)
            name = wheel.filename

            if ("/" in name) or ("\\" in name) or any(((ord(character) < 32) or (ord(character) == 127)) for character in name):
                raise ValueError(f"{package.name=!s}: unsafe wheel filename {name=!r}")

            digest = wheel.hashes.get("sha256")

            if not _sha256_text(digest) or not _sha256_text(evidence.get(digest)):
                raise ValueError(f"{name=!s} from {wheel.url=!s}: required artifact hash or metadata evidence is absent")

    return (envelope, document, list(document.select()))


def missing_wheels(artifacts, evidence, directory_fd):
    missing = []

    for (package, wheel) in artifacts:
        try:
            verify_wheel(wheel.filename, wheel, evidence, directory_fd)
        except FileNotFoundError:
            missing.append((package, wheel))

    return missing


def consume(action, lock, pool, local_output=None):
    """Consume the existing selection, publishing only completely verified wheels."""
    (lock, pool) = (Path(lock), Path(pool))

    if local_output is not None:
        local_output = Path(local_output)

        if not is_valid_pylock_path(local_output):
            raise ValueError("The local projection must be named pylock.toml or pylock.<name>.toml")

        if local_output.resolve() == lock.resolve():
            raise ValueError("A disposable local projection must not replace the authoritative lock")

        if pool.resolve() in local_output.resolve().parents:
            raise ValueError("Write the disposable local projection outside the shared wheel pool")

    (envelope, document, artifacts) = load_locked(lock)
    evidence = envelope["metadata"]
    downloaded = []

    with pool_lock(pool) as directory_fd:
        missing = missing_wheels(artifacts, evidence, directory_fd)

    if missing and (action == "check"):
        missing_filenames = [wheel.filename for (_, wheel) in missing]

        raise FileNotFoundError(f"Missing locked wheels: {missing_filenames=!s}")

    if missing:
        with acquisition_lock(pool) as directory_fd:
            # Another cooperating writer may have completed while we waited.
            missing = missing_wheels(missing, evidence, directory_fd)

            with tempfile.TemporaryDirectory(prefix=".wheelhouse-", dir=pool) as temporary, network_policy():
                for (_, wheel) in missing:
                    staged = Path(temporary) / wheel.filename

                    try:
                        with urllib.request.urlopen(wheel.url, timeout=60) as source, staged.open("xb") as target:
                            shutil.copyfileobj(source, target)
                    except Exception as error:
                        raise RuntimeError(f"{wheel.filename=!s} from {wheel.url=!s}: {error=!s}") from error

                    verify_wheel(staged, wheel, evidence)
                    os.link(staged, wheel.filename, dst_dir_fd=directory_fd, follow_symlinks=False)
                    downloaded.append(wheel.filename)
                    staged.unlink()

    if local_output is not None:
        projection = local_lock(document, pool.resolve(), artifacts)

        atomic_write(local_output, projection.encode("utf-8"))

    return {"artifacts": [wheel.filename for (_, wheel) in artifacts], "downloaded": downloaded}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def read_project(path):
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

        if (requirement.url is not None) or ("\n" in text) or ("\r" in text):
            raise ValueError(f"Direct URLs and requirements-file directives are unsupported: {text=!s}")

        fields.update(marker_fields(requirement.marker))
        requirements.append(str(requirement))

    if project.get("requires-python") and not SpecifierSet(project["requires-python"]).contains(platform.python_version(), prereleases=True):
        raise ValueError(f"This interpreter does not satisfy project.requires-python: {project['requires-python']=!s}")

    settings = data.get("tool", {}).get("wheelhouse", {})

    if "index" not in settings:
        raise ValueError("Declare tool.wheelhouse.index in the project file")

    indexes = {"default": settings["index"]}

    for (name, source) in settings.get("sources", {}).items():
        normalized = canonicalize_name(name, validate=True)

        if normalized in indexes:
            raise ValueError(f"Duplicate or reserved package source: {name=!s}")

        indexes[normalized] = source

    for source in indexes.values():
        parts = validate_url(source)

        if parts.query or parts.fragment:
            raise ValueError(f"Index URLs must be plain base URLs: {source=!s}")

    return (requirements, indexes, fields)


def lock_project(project, pool, output):
    """Give uv metadata and two views of each declared publisher; never wheel bytes."""
    (project, pool, output) = map(Path, (project, pool, output))
    (requirements, indexes, marker_names) = read_project(project)

    if output.resolve().is_relative_to(pool.resolve()):
        raise ValueError("Write the project lock outside the shared wheelhouse")

    initial = {path.name for path in pool.iterdir()}
    tags = set(sys_tags())
    (artifacts, observed, failures) = ({}, {}, [])
    gate = threading.RLock()

    @lru_cache(maxsize=None)
    def cached(name, checksum):
        if (name not in initial) or (checksum is None):
            return False

        try:
            with wheel_file(pool / name, checksum):
                return True
        except FileNotFoundError:
            return False

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

            raise RuntimeError(f"Publisher index {address=!s}: {error=!s}") from error
        except Exception as error:
            raise RuntimeError(f"Publisher index {address=!s}: {error=!s}") from error

        entries = []

        for attrs in parser.links:
            source = urljoin(base, attrs.get("href") or "")
            parts = urlsplit(source)
            filename = unquote(parts.path.rsplit("/", 1)[-1])

            if not filename.endswith(".whl"):
                continue

            try:
                (name, version, _, wheel_tags) = parse_wheel_filename(filename)
            except InvalidWheelFilename:
                continue

            if ("/" in filename) or ("\\" in filename) or (name != package):
                raise ValueError(f"Wrong artifact identity from {address=!s}: {filename=!s}")

            validate_url(source)
            values = parse_qs(parts.fragment).get("sha256", [])
            checksum = values[0] if (len(values) == 1) and _sha256_text(values[0]) else None
            url = parts._replace(fragment="").geturl()

            artifact_identity = (
                f"{url}\n"
                f"{checksum}"  # machine-string: SHA-256 hashes this URL/checksum serialization; expression labels would change artifact tokens.
            )

            token = hashlib.sha256(artifact_identity.encode()).hexdigest()

            artifact = {
                "name": filename,
                "sha256": checksum,
                "url": url,
                "index": index,
                "package": name,
                "version": version,
                "tags": wheel_tags,
                "metadata": attrs.get("data-core-metadata", attrs.get("data-dist-info-metadata"))
            }

            if (token in artifacts) and (artifacts[token] != artifact):
                raise ValueError(f"Conflicting index entries for {filename=!s} at {index=!s}")

            artifacts[token] = artifact

            entries.append((token, attrs))

        return entries

    @lru_cache(maxsize=None)
    def metadata(token):
        item = artifacts[token]
        (filename, checksum) = (item["name"], item["sha256"])

        try:
            if checksum is None:
                raise ValueError("Publisher did not supply one valid artifact SHA-256")

            if cached(filename, checksum):
                with wheel_file(pool / filename) as stream:
                    data = wheel_metadata(stream)
            else:
                hint = item["metadata"]

                if not hint:
                    raise ValueError("Required separate metadata is unavailable; wheel downloads are forbidden during resolution")

                parts = urlsplit(item["url"])
                address = parts._replace(path=parts.path + ".metadata").geturl()

                with urllib.request.urlopen(address, timeout=30) as response:
                    data = response.read()

                if hint != "true":
                    (algorithm, expected) = hint.split("=", 1)

                    if hashlib.new(algorithm, data).hexdigest() != expected:
                        raise ValueError(f"Publisher metadata hash mismatch: {address=!s}")

            marker_names.update(validate_metadata(data, filename))

            return data
        except Exception as error:
            raise RuntimeError(f"Metadata for {filename=!s} from {item['index']=!s}: {error=!s}") from error

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
                    (data, content_type) = self.route(body)

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

            if (len(route) == 2) and (route[0] in ("cached", "all")):
                (kind, package) = route
                package = canonicalize_name(package, validate=True)
                links = []

                for (token, attrs) in page(package):
                    item = artifacts[token]
                    (filename, checksum) = (item["name"], item["sha256"])

                    if kind == "cached":
                        if not item["tags"].intersection(tags):
                            continue

                        if attrs.get("data-requires-python") and not SpecifierSet(attrs["data-requires-python"]).contains(platform.python_version(), prereleases=True):
                            continue

                        try:
                            if not cached(filename, checksum):
                                continue
                        except Exception as error:
                            raise RuntimeError(f"Local wheel {filename=!s} from {item['index']=!s}: {error=!s}") from error

                    flags = ''.join(
                        f' {name}="{html.escape(attrs[name] or "", quote=True)}"'  # machine-string: uv parses these HTML attributes; expression labels would change their names and values.
                        for name in ("data-requires-python", "data-yanked") if name in attrs
                    )

                    # Always force metadata requests, including explicit errors for missing sidecars.
                    flags += ' data-core-metadata="true" data-dist-info-metadata="true"'

                    fragment = (
                        f"#sha256={checksum}"  # machine-string: uv parses this URL fragment as a SHA-256 parameter; expression labels would change the hash.
                    ) if checksum else ""

                    links.append(
                        f'<a href="/wheel/{token}/{quote(filename)}{fragment}"{flags}>{html.escape(filename)}</a>'  # machine-string: uv parses these HTML wheel links; expression labels would change the artifact URLs and markup.
                    )

                return (("<!doctype html><html><body>" + "\n".join(links) + "</body></html>").encode(), "text/html")

            if (len(route) != 3) or (route[0] != "wheel") or (route[1] not in artifacts):
                raise ValueError(f"Unexpected resolver request: {self.path=!s}")

            (_, token, filename) = route
            item = artifacts[token]

            if unquote(filename) != (item["name"] + ".metadata"):
                raise RuntimeError(f"Wheel-byte request forbidden during resolution: {item['name']=!s} from {item['index']=!s}")

            data = metadata(token)

            if body:
                identity = (item["index"], item["package"], item["version"])

                observed.setdefault(identity, set()).add(hashlib.sha256(data).hexdigest())

            return (data, "application/octet-stream")

    with tempfile.TemporaryDirectory(prefix="wheelhouse-resolve-") as temporary, network_policy(metadata_only=True):
        work = Path(temporary)
        sanitized = work / "requirements.in"

        sanitized.write_text("\n".join(requirements) + "\n")
        raw = work / "pylock.toml"

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)

            thread.start()

            root = (
                f"http://127.0.0.1:{server.server_port}"  # machine-string: uv connects to this loopback URL; expression labels would change its authority.
            )

            environment = {
                key: value for (key, value) in os.environ.items()
                if not key.startswith(("UV_", "PIP_")) and (key != "VIRTUAL_ENV")
            }

            environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,::1"

            try:
                process = subprocess.run(
                    [
                        "uv",  # Delegate dependency solving to the installed resolver.
                        "--no-config",  # Ignore uv configuration outside this invocation.
                        "--no-cache",  # Prevent uv's cache from influencing pool preference.
                        "pip",  # Select uv's requirements-oriented interface.
                        "compile",  # Resolve dependencies without installing them.
                        str(sanitized),  # Use the validated requirements extracted from the project.
                        "--python",  # Resolve for the interpreter running this tool.
                        sys.executable,  # Keep interpreter selection consistent with recorded target tags.
                        "--no-python-downloads",  # Fail rather than acquire a different interpreter.
                        "--only-binary=:all:",  # Exclude source distributions and build steps.
                        "--index",  # Give the cached view precedence over the complete view.
                        root + "/cached/",  # Expose eligible wheels already present in the shared pool.
                        "--default-index",  # Use the complete view when cached candidates cannot solve.
                        root + "/all/",  # Expose the same declared publishers through the adapter.
                        "--index-strategy",  # Permit backtracking beyond the preferred cached view.
                        "unsafe-first-match",  # Prefer cached versions across two views of the same publisher.
                        "--allow-insecure-host",  # Allow HTTP only for the in-process adapter.
                        "127.0.0.1",  # Publisher requests still pass through the HTTPS policy.
                        "--no-header",  # Omit the generated command header from the lock.
                        "--quiet",  # Keep resolver progress out of the tool's JSON output.
                        "--output-file",  # Select uv's native lock output by its pylock.toml filename.
                        str(raw),  # Stage the result before validating and restoring publisher URLs.
                    ],
                    cwd=work,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
            finally:
                server.shutdown()
                thread.join()

        if failures:
            raise RuntimeError(str(failures[0])) from failures[0]

        if process.returncode:
            resolver_error = process.stderr.strip()

            raise RuntimeError(f"uv could not resolve dependencies:\n" f"{resolver_error=!s}")

        document = Pylock.from_dict(tomllib.loads(raw.read_text()))
        (evidence, packages) = ({}, [])

        for package in document.packages:
            name = package.name
            index = indexes.get(name, indexes["default"])
            identity = (index, name, package.version)
            digests = observed.get(identity, set())

            if len(digests) != 1:
                raise ValueError(f"Expected one consistent metadata digest for {name=!s} {package.version=!s} from {index=!s}")

            expected = next(iter(digests))
            wheels = []

            for wheel in registry_wheels(package):
                alias = urlsplit(wheel.url)
                parts = alias.path.strip("/").split("/")

                alias_origin = (
                    f"{alias.scheme}://{alias.netloc}"  # machine-string: The URL origin comparison requires scheme://authority syntax without expression labels.
                )

                if (alias_origin != root) or (len(parts) != 3) or (parts[0] != "wheel"):
                    raise ValueError(f"Unexpected artifact URL from uv for {name=!s}")

                item = artifacts[parts[1]]

                if (unquote(parts[2]) != item["name"]) or ((item["index"], item["package"], item["version"]) != identity):
                    raise ValueError(f"Artifact identity changed during resolution for {name=!s}")

                checksum = item["sha256"]

                if (wheel.hashes.get("sha256") != checksum) or (checksum is None):
                    raise ValueError(f"uv artifact hash disagrees with publisher for {item['name']=!s}")

                wheels.append(replace(wheel, url=item["url"], name=item["name"], upload_time=None))

                # uv may use one wheel's metadata for a version. Every retained wheel
                # must match those exact bytes before acquisition or offline use.
                evidence[checksum] = expected

            packages.append(replace(package, index=index, wheels=wheels))

        envelope = {
            "wheelhouse-lock": 1,
            "target": target_environment(marker_names),
            "resolution": replace(document, packages=packages).to_dict(),
            "metadata": evidence
        }

        encoded = (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode()

        atomic_write(output, encoded)

    return {"lock": str(output), "packages": len(packages), "wheel-downloads": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)

    for action in ("lock", "acquire", "check"):
        command = subcommands.add_parser(action)

        command.add_argument("--lock", type=Path, required=True)  # Write the authoritative lock for lock; read it for acquire/check.
        command.add_argument("--pool", type=Path, required=True)  # Use this shared wheel directory for preference, acquisition and verification.

        if action == "lock":
            command.add_argument("--project", type=Path, required=True)  # Resolve the dependencies and sources declared by this pyproject.toml.
        else:
            command.add_argument("--local-lock", type=Path)  # Optionally write a disposable pylock projection with verified local wheel paths.

    arguments = parser.parse_args(argv)

    try:
        if arguments.action == "lock":
            result = lock_project(arguments.project, arguments.pool, arguments.lock)
        else:
            result = consume(arguments.action, arguments.lock, arguments.pool, arguments.local_lock)
    except Exception as error:
        print(f"wheelhouse: {error=!s}", file=sys.stderr)

        return 1

    print(json.dumps(result, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
def network_policy(allow_test_http=False):
    class HTTPSRequests(urllib.request.BaseHandler):
        def http_request(self, request):
            validate_url(request.full_url, allow_test_http)
            return request

        https_request = http_request

    class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, stream, code, message, headers, new_url):
            try:
                validate_url(new_url, allow_test_http, previous=request.full_url)
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


def main(argv=None, *, allow_test_http=False):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    for action in ("acquire", "check"):
        command = subcommands.add_parser(action)
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--pool", type=Path, required=True)
        command.add_argument("--local-lock", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = consume(arguments.action, arguments.lock, arguments.pool,
                         arguments.local_lock, allow_test_http=allow_test_http)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"wheelhouse: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

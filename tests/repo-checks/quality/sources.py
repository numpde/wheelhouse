"""Discover authored Python relative to this checkout, wherever it is mounted."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECKER_ROOT = ROOT
REPOSITORY_ROOT = ROOT

SKIP_DIRS = {
    '.git',
    '.idea',
    '.venv',
    '.agents',
    '.codex',
    '.aws',
    'secrets',
    'node_modules',
    '__pycache__',
    'dist',
}


def source_files(*, root, patterns):
    """Exclude generated files and ported detector examples from source checks."""

    candidates = {path for pattern in patterns for path in root.glob(pattern)}

    return sorted(
        path
        for path in candidates
        if
        path.is_file() and
        (not path.is_symlink()) and
        (not SKIP_DIRS.intersection(path.relative_to(root).parts)) and
        (not path.is_relative_to(ROOT / 'tests/repo-checks/quality') or (path.name in {'sources.py', 'findings.py'}))
    )


def python_sources(*, production_only):
    """Apply conventions to application, build tools and tests alike."""

    return source_files(root=ROOT, patterns=('**/*.py',))

"""Write a deterministic zipapp using the image's hash-pinned dependencies."""

import importlib.metadata
from pathlib import Path
import sys
import zipfile

from packaging.requirements import Requirement


def build(source, output):
    """Bundle declared dependencies, including their metadata and license files."""

    members = {
        'wheelhouse.py': (source / 'wheelhouse.py').read_bytes(),
        '__main__.py': (
            b'from wheelhouse import main\n'
            b'raise SystemExit(main())\n'
        ),
    }

    for line in (source / 'requirements.txt').read_text().splitlines():
        if (not line.strip()) or line.startswith(' '):
            continue

        requirement = Requirement(line.rstrip(' \\'))
        distribution = importlib.metadata.distribution(requirement.name)

        if distribution.version not in requirement.specifier:
            raise ValueError(f'Installed dependency does not match {requirement=!s}')

        for entry in distribution.files:
            member_name = entry.as_posix()

            if (entry.suffix == '.pyc') or ('__pycache__' in entry.parts):
                continue

            if entry.is_absolute() or ('..' in entry.parts):
                raise ValueError(f'Cannot bundle a dependency file outside its package: {entry=!s}')

            if member_name in members:
                raise ValueError(f'Duplicate bundle member: {member_name=}')

            members[member_name] = distribution.locate_file(entry).read_bytes()

    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for (member_name, content) in sorted(members.items()):
            entry = zipfile.ZipInfo(member_name, (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16

            archive.writestr(entry, content)


if __name__ == '__main__':
    build(Path('/opt/wheelhouse'), sys.stdout.buffer)

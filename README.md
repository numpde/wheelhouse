# Wheelhouse

This repo helps projects **reuse downloaded Python packages and install them
reliably without internet access**.

A *wheel* is a ready-to-install Python package file. A *wheelhouse* is a shared
folder containing those files, so several projects can reuse them.

It works in three steps:

1. **Choose packages — `lock`.** Read a project’s requirements and ask uv to
   find compatible versions, preferring suitable wheels already in the shared
   folder. Fetch dependency information as needed, but no wheel files. Save the
   exact selection, download locations, and checksums in `wheelhouse.lock`.

2. **Fill the folder — `acquire`.** Follow that lock, verify existing wheels,
   and download only missing ones. Check both the package bytes and the
   dependency information before accepting a file. Conflicting files cause an
   error rather than being overwritten.

3. **Prepare installation — `check`.** Without network access, verify that every
   required wheel is present and produce a temporary lock pointing to local
   files. uv then installs from those files.

For example, two projects needing the same NumPy wheel can share one downloaded
copy. Once their locked dependencies are present, their installations can run
offline.

Each lock applies to a matching Python version and platform. The current tests
demonstrate Requests and NumPy acquisition, reuse, and offline installation.
Source builds are outside its scope; Torch/CUDA has not been qualified.

# Releasing h324

This is an experimental alpha. Software CI and installation support are separate
from physical modem compatibility. Keep unverified operating systems and modem
models identified in the README and release notes.

## One-time publishing setup

Create GitHub environments `testpypi`, `pypi` and `release`, with a maintainer
required for production approval and version-tag restrictions on `pypi` and
`release`. Configure a Trusted Publisher on **each** of TestPyPI and PyPI:

| Field | Value |
|---|---|
| Owner | `danohn` |
| Repository | `h324` |
| Workflow | `release.yml` |
| Environment | `testpypi` on TestPyPI; `pypi` on PyPI |

Use pending publishers if the package does not exist yet. This requires account
access and cannot be provisioned by adding a workflow alone. Do not add an API
token to this repository. Publishing jobs use OIDC and have no source checkout
or build step; build jobs have no publishing credentials. See the official
[PyPI Trusted Publishing instructions](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

## Release sequence

1. Update the static version, release notes and dependency compatibility. Run all
   tests and inspect third-party notices, including H.245 ASN.1 provenance.
2. Run `uv build`, `uv run scripts/audit_release.py --source .`, and audit the
   generated wheel and sdist. Install the wheel in a clean environment outside
   the checkout. The CI workflow performs the same checks on Python 3.11–3.14,
   macOS, Windows and Ubuntu 24.04. It deliberately installs no Qt or FFmpeg.
3. Dispatch **Publish Python release** with `publish_pypi=false`. Approve the
   `testpypi` environment. It uploads the distributions, retrieves the exact wheel
   from TestPyPI, compares SHA-256 with the built wheel and runs installed smoke
   checks. Runtime dependencies resolve from normal PyPI rather than mixing
   indexes.
4. Create and push `vVERSION` at the reviewed commit. Dispatch the same workflow
   on that tag with `publish_pypi=true`. Approve the production environment after
   TestPyPI verification. The tag must match project metadata. Package versions
   are immutable: changed source needs a new version if an index already has it.
5. Verify `pip install h324==VERSION` independently. Only then release the client
   that pins this library version. Preserve CI evidence and physical validation
   status in release notes.

Source and distribution audits reject local paths, private addresses, credentials,
device serial paths, recordings, captures and unallowlisted files. They supplement
review; they do not certify an absence of all possible sensitive content.

The client CI can explicitly build an `h324` Git ref before the first PyPI release.
Its production release workflow resolves the released PyPI dependency instead.

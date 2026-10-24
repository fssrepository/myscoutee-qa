# MyScoutee QA

`@fssrepository/myscoutee-qa` is a separately versioned Node.js package for
shared source-checkout QA tooling. It requires Node 22 or later; Python tools
also require Python 3. It does not contain product cases, actor identities,
application URLs or copied application fixtures.

| Directory | Purpose |
| --- | --- |
| `browser/` | Managed checkpoint startup, private manifest validation and geolocation holder |
| `providers/` | Non-delivering email receiver with explicit retry recipient |
| `mongo/` | Manual-QA fixture transport to an existing owned KVM lab's DEMO database |
| `webhooks/` | Signed callback verification and bounded fault injection |
| `matrix/` | Existing domain-matrix CSV/XLSX synchronization, with explicit input paths |
| `history/` | Read-only discovery of retained Codex QA messages |
| `scripts/` | Package source checks |

The application owns one JSON configuration: checkpoints, identities, URLs,
storage keys, API routes, selectors, fixture commands and project helper paths.
The package reads it directly; application launcher copies are unnecessary.

`python3 ../myscoutee-qa/matrix/export-ledger.py SOURCE.json OUTPUT.xlsx` renders
an explicit JSON `{sheetName: [[cell, ...], ...]}` ledger without application
inputs. Qualification ledger data and generated workbooks stay with the application.

Install the published package as `@fssrepository/myscoutee-qa@1.4.6`. A project
script can invoke `myscoutee-qa-browser --config ../config/qa/browser-session-checkpoints.json`.
The configuration's `launcher.projectRoot` is relative to that JSON file;
helper paths are relative to the resolved project root. `QA_BROWSER_SESSION_CONFIG`
can select a private checkpoint override while retaining the project's base defaults.

The package exports `browser/launcher`, `browser/geolocation`, `mongo/kvm-fixture`
and `webhooks`.
The browser CLI still requires the project's explicit fixture/configuration
inputs and stops at its configured startup handoff.

The Python Mongo adapter takes `--runtime-config` and `--database`, with the
project's own fixture script on stdin. It reads the existing lab SSH key,
known-hosts file and recorded namespace UID, verifies the ownership label, and
uses the existing Mongo StatefulSet or Deployment. It accepts only the configured
DEMO database and never starts infrastructure. `--print-demo-database` reads
configuration without contacting the lab. No E2E or microservices source module
is imported; QA and E2E retain separate fixture workflows. The backend checkpoint
adapter selects this tool through `MYSCOUTEE_QA_SOURCE` (a QA package directory,
defaulting to the sibling QA checkout).

## Release

Version and tag are independent of application/E2E releases. A `v1.4.6`-style
tag matching `package.json` triggers the existing release workflow pattern:
source/helper checks, tarball creation, GitHub Packages publication, integrity
verification and a GitHub release with the tarball and checksum.
Use `npm pack --ignore-scripts --pack-destination build/repository` to prepare
a local package. The `build` directory is ignored.

Closed accepted application QA is not replayed by package preparation or by
checking out this repository. Per-application cases and matrices stay with
their applications.

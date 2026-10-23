# MyScoutee QA

Shared source-only QA tools for MyScoutee and MyScoutee Template.

| Directory | Purpose |
| --- | --- |
| `browser/` | Managed checkpoint startup and geolocation holder |
| `providers/` | Non-delivering email provider fixture with configurable retry recipient |
| `webhooks/` | Signed callback verification, bounded fault injection and in-memory receipts |
| `history/` | Read-only discovery of retained Codex QA messages; no inferred test result |

Application cases, fixture scenarios, actor identities, URLs and deployment
configuration belong to each application. Their existing versioned launchers
supply this repository with explicit project paths. Set `MYSCOUTEE_QA_SOURCE`
to use a checkout elsewhere; the default is the sibling `myscoutee-qa` checkout.
No application stack or test starts when this repository is checked out.

A historical accepted result remains closed. Documentation cleanup does not
replay a campaign or turn an older intermediate status into new work.

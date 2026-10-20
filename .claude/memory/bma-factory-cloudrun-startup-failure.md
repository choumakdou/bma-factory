---
name: bma-factory-cloudrun-startup-failure
description: BMA Factory Cloud Run container-start failure — ROOT CAUSE was billing disabled on the project, not the code. Includes the full elimination table so it is never re-litigated.
metadata:
  type: project
---

# BMA Factory — Cloud Run container-start failure (2026-10-20)

## RESOLVED (2026-10-20 12:00)
Bobby lifted the spend cap to $10. Deploy succeeded, service live, full
end-to-end verified: /diag, /auth/status, /, /case/bel-air-m2, /generate
(artefact produced, sha256 recorded, download works), /api/cases.
Remaining: the OAuth sign-in click, and the Google Drive 15 GB quota.

## ROOT CAUSE
**Billing was disabled on GCP project `bma-factory` at ~2026-10-19 17:29-17:41 UTC.**
Every request to start a new instance was refused by Google, so the container
never ran and Cloud Run reported the generic
"failed to start and listen on the port defined provided by the PORT=8080
environment variable within the allocated timeout".

## Log timeline (from Cloud Logging, `resource.type="cloud_run_revision"`)
```
17:19:57  Uvicorn running on http://0.0.0.0:8080
17:19:57  Default STARTUP TCP probe succeeded after 1 attempt
17:20:49  GET /auth/status  200 OK      <- last good
17:20:49  GET /             200 OK
17:20:51  GET /diag         200 OK
17:20:20  Shutting down                  <- the only healthy instance died
17:29:36  Starting new instance. Reason: AUTOSCALING
17:41:00  *** The request failed because billing is disabled for this project. ***
17:41:00 -> 2026-10-20 02:29:56  15 x "billing is disabled"
```

## Why it looked like a code/image bug
An already-running instance keeps serving, so the failure only shows up as
503 on *new* instances. The generic Cloud Run message points at the container,
not at the project. `gcr.io/cloudrun/hello` "worked" only because its instance
had started before the cutoff.

## Ruled out — DO NOT RE-TEST (full table)
| Hypothesis | Test | Result |
|---|---|---|
| App code | chroot into the exact image, run the real CMD | **works** — binds 0.0.0.0:8080, 200 on /healthz + /diag |
| App code (2) | CMD swapped to stdlib-only http.server | still failed (billing) |
| Deps drift | diffed fastapi 0.148.0 vs 0.150.0 wheels | 0.150.0 changes only form/file handling — irrelevant |
| Memory | peak RSS of import | 43 MiB vs 512 MiB limit |
| Region | same image -> asia-east1 | fails identically |
| Project/quota | deployed gcr.io/cloudrun/hello | **READY** — instance predates the cutoff |
| Service account | default compute SA instead of service-ac1 | fails identically |
| Secret mount | removed --update-secrets entirely | fails identically |
| Base image drift | layer digests vs the working image | layers 00-03 **byte-identical** |
| Image config | dumped entrypoint/cmd/user/env from the registry | correct |
| Non-root | added USER 65532:65532 | still fails |

## Lesson
**When a Cloud Run service 503s with a generic container-start error, read the
Cloud Logging `textPayload` for "billing is disabled" FIRST.** It costs one
API call and it was the answer. `roles/logging.viewer` on the deploy SA is
what made it findable — Bobby granted it 2026-10-20 (IAM propagation took
~1 min; the 403 before that was propagation, not a failed grant).

## Side effects of the hunt (all harmless, all committed)
- `requirements.txt` pins 49 exact versions = the last-known-good build set
- deploy.yml no longer mounts `engine-sa-key` (dead code under OAuth2)
- deploy.yml has an explicit TCP startup probe (port 8080, 2s x 120)
- `demo/boot.py` (gist-based startup diagnostics) and `demo/serve_minimal.py`
  (isolation probe) added; Dockerfile `USER 65532:65532` added
- Full write-up in `diagnostics-20261020.md` in the repo

## Unrelated but important
The OAuth2 work is **done and was verified working** at 17:20 on 2026-10-19:
`/auth/status` returned `oauth_configured: true`, the home page showed the
"Sign in to save to Drive" banner, and the Drive path uses the valuer's
OAuth credentials (the service-account key is vestigial).

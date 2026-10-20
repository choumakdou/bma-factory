---
name: bma-factory-cloudrun-startup-failure
description: BMA Factory (valuation-forge) Cloud Run container-start failure — full elimination table, what's proven, and the one missing piece (container logs).
metadata:
  type: project
---

# BMA Factory — Cloud Run container-start failure (2026-10-20)

Live URL: `https://bma-factory-demo-bxbgttiecq-df.a.run.app` (was 503/500 for ~10h)
Repo: `choumakdou/bma-factory` · GCP project `bma-factory` · region `asia-east2`

## Symptom
Every revision of `bma-factory-demo` fails with `HealthCheckContainerError`:
"container failed to start and listen on the port defined provided by the
PORT=8080 environment variable within the allocated timeout".
API-created services instead report "Internal error occurred while performing
container health check".

## Proven NOT the cause (don't re-test these)
| Hypothesis | Test | Result |
|---|---|---|
| App code | chroot into the exact image, run the real CMD | **works** — binds 0.0.0.0:8080, 200 on /healthz + /diag |
| App code (2) | CMD swapped to stdlib-only `http.server` | still fails on Cloud Run |
| Deps drift | diffed fastapi 0.148.0 vs 0.150.0 wheels | 0.150.0 changes only form/file handling — irrelevant |
| Memory | peak RSS of import | 43 MiB vs 512 MiB limit |
| Region | same image → asia-east1 | fails identically |
| Project/quota | `gcr.io/cloudrun/hello` deployed | **READY** — project, region, billing, quota all fine |
| Service account | default compute SA instead of service-ac1 | fails identically |
| Secret mount | removed `--update-secrets` entirely | fails identically |
| Base image drift | layer digests vs the 10h-old WORKING image | layers 00-03 **byte-identical** |
| Image config | dumped entrypoint/cmd/user/env from registry | correct |
| Non-root | added `USER 65532:65532` | still fails |

**Smoking gun:** `gcr.io/cloudrun/hello` starts; `python:3.12-slim`-based images
do not — same service spec, same SA, same resources, byte-identical base
layers to the image that worked at 17:18 the previous day. The container
process never reaches its CMD (`demo/boot.py` produced no gist).

## What's missing
**The container's stdout/stderr.** Neither SA can read Cloud Logging:
`roles/logging.viewer` is absent, and neither SA can self-grant it
(`iam.serviceAccounts.setIamPolicy` and `cloudresourcemanager.setIamPolicy`
both 403). Fix: Bobby grants `roles/logging.viewer` to
`bma-factory-deployer@bma-factory.iam.gserviceaccount.com` at project level,
or pastes the Cloud Run "Container logs" tab content.

## Side effects of the hunt (all reverted or harmless)
- `requirements.txt` now pins 49 exact versions = the last-known-good build set
- deploy.yml no longer mounts `engine-sa-key` (dead code under OAuth2)
- deploy.yml has an explicit TCP startup probe (port 8080, 2s × 120)
- `demo/boot.py` (gist-based startup diagnostics) and `demo/serve_minimal.py`
  (isolation probe) added; Dockerfile `USER 65532:65532` added
- Full write-up in `diagnostics-20261020.md` in the repo

## Unrelated but important
The OAuth2 work itself is **done and was verified working** at 17:18 on
2026-10-19: `/auth/status` returned `oauth_configured: true`, the home page
showed the "Sign in to save to Drive" banner, and the Drive path uses the
valuer's OAuth credentials (service-account key is vestigial).

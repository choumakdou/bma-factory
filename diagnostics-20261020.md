# BMA Factory — Cloud Run container-start failure, 2026-10-20

## Symptom
Every revision of `bma-factory-demo` fails with:
`HealthCheckContainerError` / "The user-provided container failed to start and
listen on the port defined provided by the PORT=8080 environment variable
within the allocated timeout" (or "Internal error occurred while performing
container health check" for API-created services).

## What was RULED OUT (with evidence)
| Hypothesis | Test | Result |
|---|---|---|
| App code broken | chroot into the exact image, run the real CMD | **WORKS** — binds 0.0.0.0:8080, serves 200 on /healthz and /diag |
| App code broken (2nd) | swap CMD for a stdlib-only `http.server` | **STILL FAILS** on Cloud Run |
| Dependency drift | diff fastapi 0.148.0 vs 0.150.0 wheels | no relevant breaking change; only fastapi moved overnight |
| Memory | measure peak RSS of import | 43 MiB vs 512 MiB limit |
| Region | deploy same image to asia-east1 | fails identically |
| Project broken | deploy `gcr.io/cloudrun/hello` | **READY** — project, region, billing, quota all fine |
| Service account | deploy with default compute SA | fails identically |
| Secret mount | removed `--update-secrets` entirely | fails identically |
| Base image | rebuilt with `python:3.12-slim` + no pip + 1 file | **STILL FAILS** |
| Base image drift | compared layer digests vs the 10h-old WORKING image | layers 00-03 **byte-identical** |
| Image config | dumped entrypoint/cmd/user/env from the registry | correct (`uvicorn`, `PYTHONPATH=/app`, `PORT=8080`) |

## The smoking gun
`gcr.io/cloudrun/hello` starts. `python:3.12-slim`-based images do not.
Both use the same service spec, same SA, same resources.
The base layers are byte-identical to the image that worked 10 hours earlier.

## What is needed next
**The container's stdout/stderr.** Cloud Logging is not readable with the
deploy SA's roles (`roles/logging.viewer` is missing and neither SA can
self-grant it). Either:
1. Bobby grants `roles/logging.viewer` to
   `bma-factory-deployer@bma-factory.iam.gserviceaccount.com` at the
   project level, or
2. Bobby opens the Cloud Run "Container logs" tab and pastes the output.

`demo/boot.py` exists for exactly this: it captures the startup failure
and posts it to a GitHub gist before exec'ing uvicorn. It produced no gist,
which means the container never reached the CMD.

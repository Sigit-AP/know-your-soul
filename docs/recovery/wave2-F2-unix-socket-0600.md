# Wave-2 finding F2 — unix socket created world-writable (0666)

## Evidence

Audit F2: `uvicorn.Config(app, uds=...)` creates the socket `0666 & ~umask`
(uvicorn 0.54 explicitly `os.chmod(uds, 0o666)` after bind), so any local user
could invoke all 14 operations on the *recommended* deployment path
(`socket mode: 0o666; anonymous local connect -> 200 OK`).

## Root cause

`http_api.serve` delegated socket creation to uvicorn, whose `uds=` path
hard-codes `uds_perms = 0o666`.

## Affected global criterion

Spec 13.1 least privilege: the default local API surface must not be
world-accessible to other OS users.

## Fix applied

`http_api._bind_unix_socket` binds the socket itself, `os.chmod(path, 0o600)`
immediately after bind, listens, and `serve` hands uvicorn the file descriptor
(`uvicorn.Config(app, fd=...)`, which uvicorn supports and does not re-chmod).
The permission is set atomically with creation — no world-writable window,
independent of the ambient umask.

## Regression probes

- `tests/test_wave2_security.py::test_f2_unix_socket_created_0600_regardless_of_umask`
  (umask forced to `0o000`; resulting mode is exactly `0o600`)
- `test_f2_serve_over_prebound_socket_still_works` (uvicorn serves
  `GET /v1/health` over the pre-bound fd — non-vacuous legitimate flow)

## Rollback note

Restore the `uds=` config path; the helper is new and removable without
touching any other code.

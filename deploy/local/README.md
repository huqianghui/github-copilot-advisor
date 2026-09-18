# Local Blob contracts

These tests never create Azure resources or load `.env`. The default backend is a
wire-level in-memory transport beneath the **real async Azure Blob SDK**. The
Azurite backend uses the same gateway and contract tests against a real local
server. Every test creates and deletes only its own randomly named container.
The public Azurite development key in the fixture is not a cloud credential.

From the repository root, using the configured workspace interpreter:

```powershell
.\.venv\Scripts\python.exe -m pytest agent\tests\cloud\test_blob.py agent\tests\cloud\test_leases.py -q
```

## Docker Compose

Check the configured container commands first. Start Docker Desktop yourself if
its Linux engine is stopped; the test runner does not change services or start
Docker globally. Use a dedicated project name, never an unqualified `down` or a
global container prune:

```powershell
docker compose -p advisor-cloud-tests -f deploy\local\compose.yaml up -d --wait
.\.venv\Scripts\python.exe -m pytest agent\tests\cloud\test_blob.py agent\tests\cloud\test_leases.py --cloud-store=azurite -q
docker compose -p advisor-cloud-tests -f deploy\local\compose.yaml down
```

The service binds only to loopback: Blob port **11000**, Queue port **11001**.
Table storage is not published. Data is in-memory and disappears when this
dedicated emulator stops. Do not run another emulator on those same ports.
The health check only checks HTTP responsiveness; passing it is **not** evidence
that ETag or lease contracts pass.

## Foreground npm fallback

If Docker is unavailable, a project-local Azurite installation can serve the Blob
contracts without changing user services or globally installing packages:

```powershell
npm install --prefix .\.state\azurite-tools --no-save azurite@3.35.0
node .\.state\azurite-tools\node_modules\azurite\dist\src\blob\main.js --blobHost 127.0.0.1 --blobPort 11000 --inMemoryPersistence --skipApiVersionCheck --disableTelemetry --silent
```

Wait for “successfully listens”, then run the `--cloud-store=azurite` command from
a second terminal. Cold startup can take longer than a short readiness probe on
Windows. **Do not combine `--location` with `--inMemoryPersistence`**: Azurite
rejects that combination. Stop only this foreground process with Ctrl+C after
testing. Node 24.12 and Azurite 3.35.0 were exercised during implementation.

The test endpoint defaults to `http://127.0.0.1:11000/devstoreaccount1`. To use a
different local port, set `ADVISOR_TEST_BLOB_URL` for that terminal; non-loopback
hosts are rejected. No production account/key override is accepted by these
fixtures.

## Contract coverage and limits

- Creates use server-side `If-None-Match: *`; updates/deletes use `If-Match`.
- Only `BlobNotFound` on read is missing; authentication, permission, container,
  and server failures remain errors. Only `BlobAlreadyExists` makes create false.
- Leases target the mutable object itself, have a 60-second duration and renew
  every 20 seconds. A bounded renewal failure cancels protected work and raises
  `LeaseLost`; cleanup preserves independent cancellation and body errors.
- A lease on a marker cannot authorize a different blob. After a server-side
  lease break and reacquisition, the previous owner's writes/deletes are denied.
- Deterministic renewal tests trigger the heartbeat wait rather than sleeping for
  20 wall-clock seconds. The lease acquire/renew/break/release and conditional
  mutations still go through the SDK and the selected server.
- Injected 401/403/5xx responses exercise SDK error handling on both backends.
  Azurite validates storage protocol behavior, not Azure RBAC, managed identity,
  platform availability, or a deployed end-to-end advisor.

`BlobGateway(ContainerClient)` owns the container client and exposes `aclose()`;
close it before closing separately owned `AzureCredentials`. It does not create
containers or acquire credentials implicitly. Configure the injected client at
the host boundary (including its endpoint, authentication and retry policy).

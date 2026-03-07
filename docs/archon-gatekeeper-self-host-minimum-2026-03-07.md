# Archon Gatekeeper Self-Host — Minimum Bring-Up Notes

Date: 2026-03-07
Purpose: reduce ambiguity on the Hub ↔ Archon DID integration lane if we stop waiting for a preferred external Gatekeeper URL.

## What is verified

From upstream Archon source checked locally in `tmp-archon/`:

- Gatekeeper server package: `services/gatekeeper/server/package.json`
- Gatekeeper env docs: `services/gatekeeper/server/README.md`
- Runtime config: `services/gatekeeper/server/src/config.js`

## Minimum local reality on this host

The upstream quick start assumes Docker-friendly bring-up.
On this host, Docker is absent, so self-host is possible but **not one-command**.

At minimum, a working local Gatekeeper path needs:

1. **Gatekeeper server**
   - default port: `4224`
   - default bind: `0.0.0.0`

2. **IPFS API**
   - default expected URL: `http://localhost:5001/api/v0`

3. **One DID database adapter**
   - supported by docs/config: `redis`, `json`, `mongodb`, `sqlite`
   - upstream default is `redis`

## Most relevant env vars

- `ARCHON_GATEKEEPER_PORT` → default `4224`
- `ARCHON_GATEKEEPER_DB` → default `redis`
- `ARCHON_IPFS_URL` → default `http://localhost:5001/api/v0`
- `ARCHON_BIND_ADDRESS` → default `0.0.0.0`
- `ARCHON_GATEKEEPER_DID_PREFIX` → default `did:cid`
- `ARCHON_ADMIN_API_KEY` → optional admin auth
- `ARCHON_GATEKEEPER_FALLBACK_URL` → upstream default `https://dev.uniresolver.io`

## Fastest realistic self-host path here

If `hex` does not send a preferred Gatekeeper URL, the lowest-schlep path is:

1. choose a non-Redis adapter (`sqlite` or `json`) to avoid adding Redis first
2. stand up local IPFS API
3. run Gatekeeper server on `:4224`
4. point Hub at it with:
   - `ARCHON_GATEKEEPER_URL=http://127.0.0.1:4224`

## Hub side is already ready

Hub no longer hardcodes a single Gatekeeper endpoint.
It now supports:

- `ARCHON_GATEKEEPER_URL`
- `ARCHON_LEGACY_NODES`

So the remaining blocker is operational bring-up choice, not Hub code.

## Decision rule

- If `hex` sends a trusted public Gatekeeper URL → use it now.
- Otherwise → self-host with the lightest local stack, starting from a single local Gatekeeper + IPFS + minimal DB adapter.

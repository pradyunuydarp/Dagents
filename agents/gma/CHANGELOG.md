# GMA Changelog

## Unreleased

- Added `GMA_FEDERATION_JOINABLE_SITES`: sites that may get their address while the GMA runs, each at one listed address
- Added `PUT /api/v1/federation/sites/{site_id}/address` and `GET /api/v1/federation/site-addresses`, with their short forms
- Added initial Global Monitoring Agent scaffold
- Added layered package layout and placeholder FastAPI entrypoint

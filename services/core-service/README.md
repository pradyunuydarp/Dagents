# Dagents Core Service

Framework-level service catalog and topology endpoint for Dagents deployments.

## Purpose

This service gives cloud deployments a stable framework entrypoint for:

- health checks
- service discovery metadata
- runtime topology inspection

## API

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`

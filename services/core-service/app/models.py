"""Pydantic API models for the core service."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    transport: str


class ServiceDescriptor(BaseModel):
    name: str
    kind: str
    base_url: str


class ServiceCatalogResponse(BaseModel):
    services: list[ServiceDescriptor]


class TopologyResponse(BaseModel):
    framework: str
    services: list[ServiceDescriptor]

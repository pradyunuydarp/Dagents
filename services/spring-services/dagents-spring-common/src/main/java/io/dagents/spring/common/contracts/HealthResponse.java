package io.dagents.spring.common.contracts;

public record HealthResponse(
    String status,
    String service,
    String environment,
    String transport
) {}

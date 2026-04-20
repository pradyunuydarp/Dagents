package io.dagents.spring.common.contracts;

public record ServiceDescriptor(
    String name,
    String kind,
    String baseUrl
) {}

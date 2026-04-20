package io.dagents.spring.common.contracts;

public record WorkloadPort(
    String name,
    int containerPort
) {}

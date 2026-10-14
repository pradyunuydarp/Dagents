package io.dagents.spring.common.contracts;

/**
 * A port the container listens on.
 *
 * @param name the port's name
 * @param containerPort the port number; a generated Service exposes the same number
 */
public record WorkloadPort(
    String name,
    int containerPort
) {}

package io.dagents.spring.common.contracts;

/**
 * An environment variable for the container.
 *
 * @param name the variable's name
 * @param value its value
 */
public record WorkloadEnvironmentVariable(
    String name,
    String value
) {}

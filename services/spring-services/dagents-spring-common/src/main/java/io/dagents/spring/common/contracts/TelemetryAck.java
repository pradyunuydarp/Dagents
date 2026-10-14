package io.dagents.spring.common.contracts;

/**
 * The answer to a telemetry batch.
 *
 * @param ack always {@code true}
 * @param ingestionId {@code telemetry-<n>}, where n counts every batch stored since the service
 *     started, from all agents
 */
public record TelemetryAck(
    boolean ack,
    String ingestionId
) {}

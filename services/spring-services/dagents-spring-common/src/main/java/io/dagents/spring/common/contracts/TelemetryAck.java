package io.dagents.spring.common.contracts;

public record TelemetryAck(
    boolean ack,
    String ingestionId
) {}

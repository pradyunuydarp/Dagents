package io.dagents.spring.common.contracts;

public record HeartbeatResponse(
    boolean ack,
    String desiredState
) {}

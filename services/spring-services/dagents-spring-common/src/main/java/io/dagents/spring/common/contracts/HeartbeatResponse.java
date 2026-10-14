package io.dagents.spring.common.contracts;

/**
 * The answer to a heartbeat.
 *
 * @param ack always {@code true}
 * @param desiredState {@code SYNC_REQUIRED} when a desired bundle is set and the agent last
 *     reported another bundle id or version; otherwise {@code ACTIVE}
 */
public record HeartbeatResponse(
    boolean ack,
    String desiredState
) {}

package io.dagents.spring.common.contracts;

import java.util.List;
import java.util.Map;

/**
 * What the control service knows about one agent.
 *
 * @param agent who the agent is
 * @param scope from the registration; empty when the agent never registered
 * @param version from the registration; {@code unknown} when the agent never registered
 * @param capabilities from the registration
 * @param status {@code REGISTERED} after the first registration, then the status of the last
 *     heartbeat
 * @param lastSeenAt the {@code timestamp} of the last heartbeat, or null
 * @param lastHeartbeatStatus the status of the last heartbeat, or null
 * @param lastIngestionId the {@code ingestionId} of the last telemetry batch, or null
 * @param desiredBundleId the bundle of the last desired deployment, or null
 * @param desiredBundleVersion its version, or null
 * @param currentBundleId the bundle the agent last reported running, or null
 * @param currentBundleVersion its version, or null
 */
public record AgentSnapshot(
    AgentIdentity agent,
    Map<String, String> scope,
    String version,
    List<String> capabilities,
    String status,
    Long lastSeenAt,
    String lastHeartbeatStatus,
    String lastIngestionId,
    String desiredBundleId,
    String desiredBundleVersion,
    String currentBundleId,
    String currentBundleVersion
) {}

package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;

/**
 * An agent reports the bundle it runs now.
 *
 * @param agent who the agent is; its {@code agentId} must equal the id in the path
 * @param bundleId the bundle the agent runs; stored as {@code currentBundleId}
 * @param bundleVersion its version; stored as {@code currentBundleVersion}
 */
public record DeploymentSyncRequest(
    @Valid AgentIdentity agent,
    String bundleId,
    String bundleVersion
) {}

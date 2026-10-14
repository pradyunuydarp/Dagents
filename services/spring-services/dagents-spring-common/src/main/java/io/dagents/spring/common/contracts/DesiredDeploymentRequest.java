package io.dagents.spring.common.contracts;

import java.util.Map;

/**
 * The bundle an agent should run.
 *
 * @param agentId the agent; must equal the id in the path
 * @param bundleId the bundle to run
 * @param bundleVersion its version
 * @param bundleUri where the agent can fetch it; stored, not fetched
 * @param config settings for the bundle, as text values
 */
public record DesiredDeploymentRequest(
    String agentId,
    String bundleId,
    String bundleVersion,
    String bundleUri,
    Map<String, String> config
) {}

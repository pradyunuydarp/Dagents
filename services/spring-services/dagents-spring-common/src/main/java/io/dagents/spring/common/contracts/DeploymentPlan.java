package io.dagents.spring.common.contracts;

import java.util.Map;

/**
 * The desired deployment stored for one agent. A new plan for the agent replaces the old one.
 *
 * @param agentId the agent
 * @param bundleId the bundle to run
 * @param bundleVersion its version
 * @param bundleUri where to fetch it
 * @param config settings for the bundle
 * @param planToken {@code plan-} and the first 12 characters of {@code configDigest}
 * @param configDigest the SHA-256, in hex, of the agent id, bundle id, version, URI and config
 *     joined with colons, so the same request gives the same digest
 * @param createdAt when the plan was stored, in seconds since 1970
 */
public record DeploymentPlan(
    String agentId,
    String bundleId,
    String bundleVersion,
    String bundleUri,
    Map<String, String> config,
    String planToken,
    String configDigest,
    long createdAt
) {}

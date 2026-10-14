package io.dagents.spring.common.contracts;

/**
 * Whether the agent runs the desired bundle, and what that bundle is.
 *
 * @param upToDate {@code true} when the reported bundle id and version equal the desired ones, or
 *     when no deployment is planned
 * @param planToken the plan's token, or an empty string when no deployment is planned
 * @param configDigest the plan's digest, or an empty string when no deployment is planned
 * @param desiredBundleId the bundle to run, or null when no deployment is planned
 * @param desiredBundleVersion its version, or null
 * @param desiredBundleUri where to fetch it, or null
 */
public record DeploymentSyncResponse(
    boolean upToDate,
    String planToken,
    String configDigest,
    String desiredBundleId,
    String desiredBundleVersion,
    String desiredBundleUri
) {}

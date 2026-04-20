package io.dagents.spring.common.contracts;

import java.util.Map;

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

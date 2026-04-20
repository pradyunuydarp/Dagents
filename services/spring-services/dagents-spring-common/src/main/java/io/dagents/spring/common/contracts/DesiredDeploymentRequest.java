package io.dagents.spring.common.contracts;

import java.util.Map;

public record DesiredDeploymentRequest(
    String agentId,
    String bundleId,
    String bundleVersion,
    String bundleUri,
    Map<String, String> config
) {}

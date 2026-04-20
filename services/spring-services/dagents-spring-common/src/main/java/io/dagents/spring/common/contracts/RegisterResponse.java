package io.dagents.spring.common.contracts;

import java.util.List;

public record RegisterResponse(
    boolean accepted,
    String deploymentPlanId,
    List<String> capabilities
) {}

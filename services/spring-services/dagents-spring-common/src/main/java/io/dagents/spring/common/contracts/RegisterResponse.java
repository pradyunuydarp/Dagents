package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * The answer to a registration.
 *
 * @param accepted always {@code true}
 * @param deploymentPlanId the {@code planToken} of the desired deployment stored for this agent,
 *     or null when there is none
 * @param capabilities the capabilities from the request
 */
public record RegisterResponse(
    boolean accepted,
    String deploymentPlanId,
    List<String> capabilities
) {}

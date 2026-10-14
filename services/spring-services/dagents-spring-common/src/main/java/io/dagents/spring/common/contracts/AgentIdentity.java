package io.dagents.spring.common.contracts;

import jakarta.validation.constraints.NotBlank;

/**
 * Who an agent is. Sent inside every agent control request.
 *
 * @param agentId the agent's id, and the key the registry stores it under; must not be blank where
 *     the request is validated
 * @param workspaceId the workspace the agent belongs to; stored and returned, not read
 * @param name a display name; stored and returned, not read
 * @param agentType the kind of agent, such as {@code LMA}; stored and returned, not read
 */
public record AgentIdentity(
    @NotBlank String agentId,
    @NotBlank String workspaceId,
    @NotBlank String name,
    @NotBlank String agentType
) {}

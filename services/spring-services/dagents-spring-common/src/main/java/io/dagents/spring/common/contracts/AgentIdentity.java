package io.dagents.spring.common.contracts;

import jakarta.validation.constraints.NotBlank;

public record AgentIdentity(
    @NotBlank String agentId,
    @NotBlank String workspaceId,
    @NotBlank String name,
    @NotBlank String agentType
) {}

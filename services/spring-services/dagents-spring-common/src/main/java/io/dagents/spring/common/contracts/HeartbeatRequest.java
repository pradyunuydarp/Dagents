package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;

public record HeartbeatRequest(
    @Valid AgentIdentity agent,
    @NotBlank String status,
    long timestamp
) {}

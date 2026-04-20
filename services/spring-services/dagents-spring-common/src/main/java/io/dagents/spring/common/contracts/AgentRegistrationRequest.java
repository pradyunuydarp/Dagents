package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import java.util.List;
import java.util.Map;

public record AgentRegistrationRequest(
    @Valid AgentIdentity agent,
    Map<String, String> scope,
    @NotBlank String version,
    List<String> capabilities
) {}

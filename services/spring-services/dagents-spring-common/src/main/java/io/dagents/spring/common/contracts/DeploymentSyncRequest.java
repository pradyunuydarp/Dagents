package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;

public record DeploymentSyncRequest(
    @Valid AgentIdentity agent,
    String bundleId,
    String bundleVersion
) {}

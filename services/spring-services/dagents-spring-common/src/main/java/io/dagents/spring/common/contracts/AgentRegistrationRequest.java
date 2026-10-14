package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import java.util.List;
import java.util.Map;

/**
 * A request to register an agent, or to update its registration.
 *
 * @param agent who the agent is; its {@code agentId} must equal the id in the path
 * @param scope labels for what the agent covers, such as {@code store: north}; stored and returned
 * @param version the agent's software version; must not be blank
 * @param capabilities what the agent can do, such as {@code profile}; stored and echoed back
 */
public record AgentRegistrationRequest(
    @Valid AgentIdentity agent,
    Map<String, String> scope,
    @NotBlank String version,
    List<String> capabilities
) {}

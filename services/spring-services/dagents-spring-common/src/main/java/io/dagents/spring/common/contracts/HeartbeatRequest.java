package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;

/**
 * A heartbeat: the agent says it is alive and in which state.
 *
 * @param agent who the agent is; its {@code agentId} must equal the id in the path
 * @param status the agent's state, such as {@code ACTIVE}; must not be blank. It becomes the
 *     agent's {@code status} and {@code lastHeartbeatStatus}
 * @param timestamp when the agent sent it, as the agent counts time; stored as {@code lastSeenAt}
 *     and not checked
 */
public record HeartbeatRequest(
    @Valid AgentIdentity agent,
    @NotBlank String status,
    long timestamp
) {}

package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;

/**
 * A batch of metrics from one agent.
 *
 * @param agent who the agent is; its {@code agentId} must equal the id in the path
 * @param metrics the measurements; stored as sent
 * @param pointers names of places where more detail is kept, such as a dashboard; stored, not read
 */
public record TelemetryEnvelope(
    @Valid AgentIdentity agent,
    List<TelemetryPoint> metrics,
    Map<String, String> pointers
) {}

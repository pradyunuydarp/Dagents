package io.dagents.spring.common.contracts;

import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;

public record TelemetryEnvelope(
    @Valid AgentIdentity agent,
    List<TelemetryPoint> metrics,
    Map<String, String> pointers
) {}

package io.dagents.spring.common.contracts;

import java.util.Map;

public record TelemetryPoint(
    String key,
    double value,
    Map<String, String> labels,
    long observedAt
) {}

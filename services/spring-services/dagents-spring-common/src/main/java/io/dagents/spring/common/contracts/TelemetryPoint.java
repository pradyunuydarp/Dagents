package io.dagents.spring.common.contracts;

import java.util.Map;

/**
 * One measurement in a telemetry batch.
 *
 * @param key what was measured, such as {@code orders_per_minute}
 * @param value the measured value
 * @param labels extra labels for the measurement
 * @param observedAt when it was measured, as the agent counts time
 */
public record TelemetryPoint(
    String key,
    double value,
    Map<String, String> labels,
    long observedAt
) {}

package io.dagents.spring.common.contracts;

/**
 * The answer to a health check.
 *
 * @param status always {@code ok} when the service answers
 * @param service the service name, from {@code DAGENTS_CONTROL_APP_NAME} or
 *     {@code DAGENTS_CORE_APP_NAME}
 * @param environment the deployment environment, from {@code DAGENTS_CONTROL_ENVIRONMENT} or
 *     {@code DAGENTS_CORE_ENVIRONMENT}
 * @param transport always {@code http}
 */
public record HealthResponse(
    String status,
    String service,
    String environment,
    String transport
) {}

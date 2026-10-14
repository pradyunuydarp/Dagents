package io.dagents.spring.common.contracts;

/**
 * One service in the catalog.
 *
 * @param name the service's name
 * @param kind {@code agent} for the LMA and GMA, {@code service} for the others
 * @param baseUrl the address from the matching {@code DAGENTS_CORE_*_URL} setting; it is not
 *     checked
 */
public record ServiceDescriptor(
    String name,
    String kind,
    String baseUrl
) {}

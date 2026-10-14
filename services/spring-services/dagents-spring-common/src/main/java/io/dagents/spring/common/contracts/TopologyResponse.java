package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * The catalog with the framework's name.
 *
 * @param framework always {@code dagents}
 * @param services the same list as the service catalog
 */
public record TopologyResponse(
    String framework,
    List<ServiceDescriptor> services
) {}

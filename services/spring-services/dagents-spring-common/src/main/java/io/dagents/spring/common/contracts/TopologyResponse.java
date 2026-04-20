package io.dagents.spring.common.contracts;

import java.util.List;

public record TopologyResponse(
    String framework,
    List<ServiceDescriptor> services
) {}

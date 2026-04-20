package io.dagents.spring.common.contracts;

import java.util.List;

public record WorkloadCompileRequest(
    String planId,
    String namespace,
    List<WorkloadComponent> components,
    boolean includeServices,
    boolean includeConfigMaps
) {}

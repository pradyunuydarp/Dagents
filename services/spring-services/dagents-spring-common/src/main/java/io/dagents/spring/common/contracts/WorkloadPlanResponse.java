package io.dagents.spring.common.contracts;

import java.util.List;

public record WorkloadPlanResponse(
    String planId,
    String namespace,
    List<WorkloadManifest> manifests,
    String combinedYaml
) {}

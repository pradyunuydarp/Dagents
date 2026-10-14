package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * A compiled workload plan, as stored and returned.
 *
 * @param planId the id the plan is stored under
 * @param namespace the namespace of every object
 * @param manifests one entry per component, in request order
 * @param combinedYaml every object in one YAML document, separated by {@code ---}
 */
public record WorkloadPlanResponse(
    String planId,
    String namespace,
    List<WorkloadManifest> manifests,
    String combinedYaml
) {}

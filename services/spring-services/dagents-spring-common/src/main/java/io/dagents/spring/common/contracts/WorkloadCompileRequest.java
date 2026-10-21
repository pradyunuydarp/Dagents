package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * A request to render Kubernetes YAML for a list of workloads.
 *
 * @param planId the id to store the plan under. When it is null or left out, the planner uses
 *     {@code dagents-plan}; an empty id stays empty. The Java renderer uses
 *     {@code spring-workload-plan-<milliseconds since 1970>} for a null, empty or blank id
 * @param namespace the Kubernetes namespace. When it is null or left out, the planner uses
 *     {@code dagents} and the Java renderer writes {@code null}; an empty namespace stays empty
 * @param components the workloads to render
 * @param includeServices give each component that has ports a Service; {@code false} when left out
 * @param includeConfigMaps give each component a ConfigMap; {@code false} when left out
 */
public record WorkloadCompileRequest(
    String planId,
    String namespace,
    List<WorkloadComponent> components,
    boolean includeServices,
    boolean includeConfigMaps
) {}

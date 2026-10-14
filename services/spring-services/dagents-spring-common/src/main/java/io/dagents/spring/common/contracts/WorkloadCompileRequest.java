package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * A request to render Kubernetes YAML for a list of workloads.
 *
 * @param planId the id to store the plan under. When it is empty, the planner uses
 *     {@code dagents-plan} and the Java renderer
 *     {@code spring-workload-plan-<milliseconds since 1970>}
 * @param namespace the Kubernetes namespace; when it is empty the planner uses {@code dagents} and
 *     the Java renderer writes {@code null}
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

package io.dagents.spring.common.contracts;

public record WorkloadManifest(
    String componentName,
    String kind,
    String deploymentYaml,
    String serviceYaml,
    String configMapYaml
) {}

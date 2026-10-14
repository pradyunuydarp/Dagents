package io.dagents.spring.common.contracts;

/**
 * The YAML rendered for one component.
 *
 * @param componentName the component
 * @param kind the kind of the main object
 * @param deploymentYaml the main object: a Deployment, Job or CronJob
 * @param serviceYaml the Service, or null when none was rendered
 * @param configMapYaml the ConfigMap, or null when none was rendered
 */
public record WorkloadManifest(
    String componentName,
    String kind,
    String deploymentYaml,
    String serviceYaml,
    String configMapYaml
) {}

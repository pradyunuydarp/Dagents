package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * One workload to render: a container image and how Kubernetes runs it.
 *
 * @param name the Kubernetes object name and the {@code app} label
 * @param image the container image
 * @param kind {@code Deployment}, {@code Job} or {@code CronJob}; empty means {@code Deployment}
 * @param replicas pods for a Deployment; a value below 1 becomes 1
 * @param schedule the cron schedule of a CronJob; empty means {@code 0 * * * *}
 * @param ports ports the container listens on
 * @param env environment variables
 * @param args arguments for the container; when it is left out the planner fails and the Java
 *     renderer answers
 * @param resources CPU and memory; empty means 250m and 1 CPU, 256Mi and 1Gi memory
 */
public record WorkloadComponent(
    String name,
    String image,
    String kind,
    int replicas,
    String schedule,
    List<WorkloadPort> ports,
    List<WorkloadEnvironmentVariable> env,
    List<String> args,
    WorkloadResources resources
) {}

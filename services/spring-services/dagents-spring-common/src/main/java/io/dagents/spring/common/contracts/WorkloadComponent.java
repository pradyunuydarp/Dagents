package io.dagents.spring.common.contracts;

import java.util.List;

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

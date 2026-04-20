package io.dagents.spring.common.contracts;

public record WorkloadResources(
    String cpuRequest,
    String cpuLimit,
    String memoryRequest,
    String memoryLimit
) {
  public static WorkloadResources defaults() {
    return new WorkloadResources("250m", "1", "256Mi", "1Gi");
  }
}

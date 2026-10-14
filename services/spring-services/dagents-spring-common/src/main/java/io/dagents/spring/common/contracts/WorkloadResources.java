package io.dagents.spring.common.contracts;

/**
 * CPU and memory requests and limits.
 *
 * @param cpuRequest CPU the scheduler reserves
 * @param cpuLimit most CPU the container may use
 * @param memoryRequest memory the scheduler reserves
 * @param memoryLimit most memory the container may use
 */
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

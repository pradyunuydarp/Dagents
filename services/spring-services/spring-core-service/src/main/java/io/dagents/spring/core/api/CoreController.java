package io.dagents.spring.core.api;

import io.dagents.spring.common.contracts.HealthResponse;
import io.dagents.spring.common.contracts.ServiceCatalogResponse;
import io.dagents.spring.common.contracts.TopologyResponse;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.core.application.CoreFacadeService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1")
public class CoreController {
  private final CoreFacadeService coreFacadeService;

  public CoreController(CoreFacadeService coreFacadeService) {
    this.coreFacadeService = coreFacadeService;
  }

  /**
   * Report that the core service is running.
   */
  @GetMapping("/health")
  public HealthResponse health() {
    return coreFacadeService.health();
  }

  /**
   * List the five services this service is configured with.
   */
  @GetMapping("/services")
  public ServiceCatalogResponse services() {
    return coreFacadeService.services();
  }

  /**
   * Return the service list with the framework's name.
   */
  @GetMapping("/topology")
  public TopologyResponse topology() {
    return coreFacadeService.topology();
  }

  /**
   * Render Kubernetes YAML for a list of workloads and store the plan.
   *
   * <p>When {@code DAGENTS_OCAML_MANIFEST_CLI} names the planner, the gateway runs
   * {@code dagentsc manifest compile} and returns the planner's plan. Without that setting, or
   * when the planner fails, the Java renderer answers instead and the service logs why.
   *
   * @param request the workloads
   */
  @PostMapping("/workloads:compile")
  public WorkloadPlanResponse compile(@RequestBody WorkloadCompileRequest request) {
    return coreFacadeService.compile(request);
  }

  /**
   * Fetch a stored workload plan.
   *
   * <p>An unknown id answers 200 with an empty body.
   *
   * @param planId the {@code planId} a compile returned
   */
  @GetMapping("/workload-plans/{planId}")
  public WorkloadPlanResponse plan(@PathVariable String planId) {
    return coreFacadeService.getPlan(planId);
  }
}

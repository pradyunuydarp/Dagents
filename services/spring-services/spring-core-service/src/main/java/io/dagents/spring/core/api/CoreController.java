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

  @GetMapping("/health")
  public HealthResponse health() {
    return coreFacadeService.health();
  }

  @GetMapping("/services")
  public ServiceCatalogResponse services() {
    return coreFacadeService.services();
  }

  @GetMapping("/topology")
  public TopologyResponse topology() {
    return coreFacadeService.topology();
  }

  @PostMapping("/workloads:compile")
  public WorkloadPlanResponse compile(@RequestBody WorkloadCompileRequest request) {
    return coreFacadeService.compile(request);
  }

  @GetMapping("/workload-plans/{planId}")
  public WorkloadPlanResponse plan(@PathVariable String planId) {
    return coreFacadeService.getPlan(planId);
  }
}

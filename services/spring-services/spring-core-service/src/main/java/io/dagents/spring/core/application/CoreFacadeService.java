package io.dagents.spring.core.application;

import io.dagents.spring.common.contracts.HealthResponse;
import io.dagents.spring.common.contracts.ServiceCatalogResponse;
import io.dagents.spring.common.contracts.ServiceDescriptor;
import io.dagents.spring.common.contracts.TopologyResponse;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.core.compiler.ManifestCompilerGateway;
import io.dagents.spring.core.config.CoreServiceProperties;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class CoreFacadeService {
  private final CoreServiceProperties properties;
  private final ManifestCompilerGateway compilerGateway;
  private final InMemoryWorkloadPlanRepository plans;

  public CoreFacadeService(
      CoreServiceProperties properties,
      ManifestCompilerGateway compilerGateway,
      InMemoryWorkloadPlanRepository plans
  ) {
    this.properties = properties;
    this.compilerGateway = compilerGateway;
    this.plans = plans;
  }

  public HealthResponse health() {
    return new HealthResponse("ok", properties.appName(), properties.environment(), "http");
  }

  public ServiceCatalogResponse services() {
    return new ServiceCatalogResponse(descriptors());
  }

  public TopologyResponse topology() {
    return new TopologyResponse("dagents", descriptors());
  }

  public WorkloadPlanResponse compile(WorkloadCompileRequest request) {
    WorkloadPlanResponse compiled = compilerGateway.compile(request);
    return plans.save(compiled);
  }

  public WorkloadPlanResponse getPlan(String planId) {
    return plans.find(planId);
  }

  private List<ServiceDescriptor> descriptors() {
    return List.of(
        new ServiceDescriptor("spring-control-service", "service", properties.controlServiceUrl()),
        new ServiceDescriptor("lma", "agent", properties.lmaUrl()),
        new ServiceDescriptor("gma", "agent", properties.gmaUrl()),
        new ServiceDescriptor("model-service", "service", properties.modelServiceUrl()),
        new ServiceDescriptor("pipeline-service", "service", properties.pipelineServiceUrl())
    );
  }
}

package io.dagents.spring.core.api;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import io.dagents.spring.common.contracts.WorkloadManifest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.core.application.CoreFacadeService;
import io.dagents.spring.core.application.InMemoryWorkloadPlanRepository;
import io.dagents.spring.core.compiler.ManifestCompilerGateway;
import io.dagents.spring.core.config.CoreServiceProperties;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class CoreControllerTest {
  private MockMvc mockMvc;

  @BeforeEach
  void setUp() {
    CoreServiceProperties properties = new CoreServiceProperties(
        "dagents-spring-core-service",
        "test",
        "http://spring-control-service:8050",
        "http://lma:8010",
        "http://gma:8020",
        "http://model-service:8000",
        "http://pipeline-service:8030"
    );
    ManifestCompilerGateway gateway = request -> new WorkloadPlanResponse(
        request.planId(),
        request.namespace(),
        List.of(new WorkloadManifest("core", "Deployment", "kind: Deployment", "kind: Service", "kind: ConfigMap")),
        "kind: Deployment\n---\nkind: Service"
    );
    CoreFacadeService service = new CoreFacadeService(properties, gateway, new InMemoryWorkloadPlanRepository());
    mockMvc = MockMvcBuilders.standaloneSetup(new CoreController(service))
        .setControllerAdvice(new ApiExceptionHandler())
        .build();
  }

  @Test
  void compilesWorkloadsViaRest() throws Exception {
    mockMvc.perform(post("/api/v1/workloads:compile")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {
                  "planId": "plan-1",
                  "namespace": "dagents",
                  "components": [
                    {
                      "name": "core",
                      "image": "ghcr.io/example/core:latest",
                      "kind": "Deployment",
                      "replicas": 1,
                      "ports": [{"name": "http", "containerPort": 8060}],
                      "env": [{"name": "APP_ENV", "value": "test"}],
                      "args": ["--server.port=8060"],
                      "resources": {
                        "cpuRequest": "250m",
                        "cpuLimit": "1",
                        "memoryRequest": "256Mi",
                        "memoryLimit": "1Gi"
                      }
                    }
                  ],
                  "includeServices": true,
                  "includeConfigMaps": true
                }
                """))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.planId").value("plan-1"))
        .andExpect(jsonPath("$.manifests[0].kind").value("Deployment"));
  }

  @Test
  void returnsStoredPlan() throws Exception {
    mockMvc.perform(post("/api/v1/workloads:compile")
        .contentType(MediaType.APPLICATION_JSON)
        .content("""
            {
              "planId": "plan-1",
              "namespace": "dagents",
              "components": [],
              "includeServices": true,
              "includeConfigMaps": true
            }
            """))
        .andExpect(status().isOk());

    mockMvc.perform(get("/api/v1/workload-plans/plan-1"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.planId").value("plan-1"))
        .andExpect(jsonPath("$.combinedYaml").value("kind: Deployment\n---\nkind: Service"));
  }
}

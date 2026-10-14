package io.dagents.spring.core;

import static org.hamcrest.Matchers.containsString;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

/**
 * Starts the whole application with the settings in env/.env.spring-core-service. The controller
 * tests build the beans by hand, so they passed while the application could not start.
 *
 * <p>DAGENTS_OCAML_MANIFEST_CLI is empty here, so a value in the developer's environment does not
 * change the result. {@link ApplicationWithPlannerTest} sets it to the planner.
 */
@SpringBootTest(properties = {
    "SERVER_PORT=0",
    "DAGENTS_CORE_APP_NAME=dagents-spring-core-service",
    "DAGENTS_CORE_ENVIRONMENT=test",
    "DAGENTS_CORE_CONTROL_SERVICE_URL=http://spring-control-service:8050",
    "DAGENTS_CORE_LMA_URL=http://lma:8010",
    "DAGENTS_CORE_GMA_URL=http://gma:8020",
    "DAGENTS_CORE_MODEL_SERVICE_URL=http://model-service:8000",
    "DAGENTS_CORE_PIPELINE_SERVICE_URL=http://pipeline-service:8030",
    "DAGENTS_OCAML_MANIFEST_CLI=",
})
@AutoConfigureMockMvc
abstract class ApplicationTestSupport {
  static final String PLAN_ID = "context-plan";

  private static final String COMPILE_REQUEST = """
      {
        "planId": "context-plan",
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
      """;

  @Autowired
  MockMvc mockMvc;

  /** Posts the compile request and checks what both renderers return. */
  ResultActions compile() throws Exception {
    return mockMvc.perform(post("/api/v1/workloads:compile")
            .contentType(MediaType.APPLICATION_JSON)
            .content(COMPILE_REQUEST))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.planId").value(PLAN_ID))
        .andExpect(jsonPath("$.namespace").value("dagents"))
        .andExpect(jsonPath("$.manifests[0].componentName").value("core"))
        .andExpect(jsonPath("$.manifests[0].kind").value("Deployment"))
        .andExpect(jsonPath("$.manifests[0].deploymentYaml").value(containsString("image: ghcr.io/example/core:latest")))
        .andExpect(jsonPath("$.manifests[0].serviceYaml").value(containsString("kind: Service")))
        .andExpect(jsonPath("$.manifests[0].configMapYaml").value(containsString("kind: ConfigMap")));
  }
}

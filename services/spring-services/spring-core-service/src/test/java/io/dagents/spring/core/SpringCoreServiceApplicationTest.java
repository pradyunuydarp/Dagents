package io.dagents.spring.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import io.dagents.spring.common.contracts.HealthResponse;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;

/**
 * Starts the whole application, as {@code java -jar} does. The other tests build
 * their objects by hand, so they pass even when Spring cannot wire the beans.
 */
@SpringBootTest(
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    // application.yml has no defaults, so these mirror env/.env.spring-core-service.
    properties = {
        "SERVER_PORT=0",
        "DAGENTS_CORE_APP_NAME=dagents-spring-core-service",
        "DAGENTS_CORE_ENVIRONMENT=test",
        "DAGENTS_CORE_CONTROL_SERVICE_URL=http://spring-control-service:8050",
        "DAGENTS_CORE_LMA_URL=http://lma:8010",
        "DAGENTS_CORE_GMA_URL=http://gma:8020",
        "DAGENTS_CORE_MODEL_SERVICE_URL=http://model-service:8000",
        "DAGENTS_CORE_PIPELINE_SERVICE_URL=http://pipeline-service:8030"
    }
)
class SpringCoreServiceApplicationTest {
  @Autowired
  private TestRestTemplate rest;

  @Test
  void answersHealthCheck() {
    ResponseEntity<HealthResponse> response = rest.getForEntity("/api/v1/health", HealthResponse.class);

    assertEquals(HttpStatus.OK, response.getStatusCode());
    assertNotNull(response.getBody());
    assertEquals("ok", response.getBody().status());
    assertEquals("dagents-spring-core-service", response.getBody().service());
  }

  @Test
  void returnsThePlanItCompiled() {
    WorkloadCompileRequest request = new WorkloadCompileRequest("plan-startup", "dagents", List.of(), true, true);

    ResponseEntity<WorkloadPlanResponse> compiled =
        rest.postForEntity("/api/v1/workloads:compile", request, WorkloadPlanResponse.class);
    assertEquals(HttpStatus.OK, compiled.getStatusCode());

    ResponseEntity<WorkloadPlanResponse> stored =
        rest.getForEntity("/api/v1/workload-plans/plan-startup", WorkloadPlanResponse.class);
    assertEquals(HttpStatus.OK, stored.getStatusCode());
    assertNotNull(stored.getBody());
    assertEquals("plan-startup", stored.getBody().planId());
    assertEquals("dagents", stored.getBody().namespace());
  }
}

package io.dagents.spring.control;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

import io.dagents.spring.common.contracts.HealthResponse;
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
    // application.yml has no defaults, so these mirror env/.env.spring-control-service.
    properties = {
        "SERVER_PORT=0",
        "DAGENTS_CONTROL_APP_NAME=dagents-spring-control-service",
        "DAGENTS_CONTROL_ENVIRONMENT=test"
    }
)
class SpringControlServiceApplicationTest {
  @Autowired
  private TestRestTemplate rest;

  @Test
  void answersHealthCheck() {
    ResponseEntity<HealthResponse> response = rest.getForEntity("/api/v1/health", HealthResponse.class);

    assertEquals(HttpStatus.OK, response.getStatusCode());
    assertNotNull(response.getBody());
    assertEquals("ok", response.getBody().status());
    assertEquals("dagents-spring-control-service", response.getBody().service());
  }
}

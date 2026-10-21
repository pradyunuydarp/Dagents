package io.dagents.spring.control;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import io.dagents.spring.control.application.ControlPlaneService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.test.web.servlet.MockMvc;

/**
 * Starts the application context with its real configuration.
 *
 * <p>The other tests build the controllers by hand, so they cannot see a configuration that
 * fails to start. The properties fill the placeholders in {@code application.yml}, which have no
 * defaults.
 */
@SpringBootTest(properties = {
    "SERVER_PORT=0",
    "DAGENTS_CONTROL_APP_NAME=spring-control-context-test",
    "DAGENTS_CONTROL_ENVIRONMENT=test"
})
@AutoConfigureMockMvc
class SpringControlServiceApplicationTest {
  @Autowired
  private ApplicationContext context;

  @Autowired
  private MockMvc mockMvc;

  @Test
  void contextLoads() {
    assertNotNull(context.getBean(ControlPlaneService.class));
  }

  @Test
  void healthAnswersFromTheRealContext() throws Exception {
    mockMvc.perform(get("/api/v1/health"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("ok"))
        .andExpect(jsonPath("$.service").value("spring-control-context-test"))
        .andExpect(jsonPath("$.environment").value("test"));
  }
}

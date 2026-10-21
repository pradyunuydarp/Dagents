package io.dagents.spring.core;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import io.dagents.spring.core.application.CoreFacadeService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.test.web.servlet.MockMvc;

/**
 * Starts the application context with its real configuration.
 *
 * <p>The other tests build the controller by hand, so a configuration that cannot start, such
 * as two beans of one type, passed every test. The properties fill the placeholders in
 * {@code application.yml}, which have no defaults; without them the context would fail for that
 * reason instead.
 */
@SpringBootTest(properties = {
    "SERVER_PORT=0",
    "DAGENTS_CORE_APP_NAME=spring-core-context-test",
    "DAGENTS_CORE_ENVIRONMENT=test",
    "DAGENTS_CORE_CONTROL_SERVICE_URL=http://127.0.0.1:9",
    "DAGENTS_CORE_LMA_URL=http://127.0.0.1:9",
    "DAGENTS_CORE_GMA_URL=http://127.0.0.1:9",
    "DAGENTS_CORE_MODEL_SERVICE_URL=http://127.0.0.1:9",
    "DAGENTS_CORE_PIPELINE_SERVICE_URL=http://127.0.0.1:9"
})
@AutoConfigureMockMvc
class SpringCoreServiceApplicationTest {
  @Autowired
  private ApplicationContext context;

  @Autowired
  private MockMvc mockMvc;

  @Test
  void contextLoads() {
    assertNotNull(context.getBean(CoreFacadeService.class));
  }

  @Test
  void healthAnswersFromTheRealContext() throws Exception {
    mockMvc.perform(get("/api/v1/health"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("ok"))
        .andExpect(jsonPath("$.service").value("spring-core-context-test"))
        .andExpect(jsonPath("$.environment").value("test"));
  }
}

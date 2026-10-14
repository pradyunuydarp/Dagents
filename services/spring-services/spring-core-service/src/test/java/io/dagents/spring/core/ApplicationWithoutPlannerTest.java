package io.dagents.spring.core;

import static org.hamcrest.Matchers.containsString;
import static org.hamcrest.Matchers.not;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;

class ApplicationWithoutPlannerTest extends ApplicationTestSupport {
  @Test
  void startsAndReportsHealth() throws Exception {
    mockMvc.perform(get("/api/v1/health"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("ok"))
        .andExpect(jsonPath("$.environment").value("test"));
  }

  @Test
  void compilesWithTheJavaRendererAndStoresThePlan() throws Exception {
    // The Java renderer leaves out the Service type; the planner writes it.
    compile().andExpect(jsonPath("$.manifests[0].serviceYaml").value(not(containsString("type: ClusterIP"))));

    mockMvc.perform(get("/api/v1/workload-plans/" + PLAN_ID))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.planId").value(PLAN_ID));
  }
}

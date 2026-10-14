package io.dagents.spring.core;

import static org.hamcrest.Matchers.containsString;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;

import io.dagents.spring.core.compiler.PlannerBinary;
import org.junit.jupiter.api.Test;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

class ApplicationWithPlannerTest extends ApplicationTestSupport {
  @DynamicPropertySource
  static void usePlanner(DynamicPropertyRegistry registry) {
    registry.add("DAGENTS_OCAML_MANIFEST_CLI", () -> PlannerBinary.path().toString());
  }

  @Test
  void compilesWithThePlanner() throws Exception {
    // Checked here, not before the application starts, so that a missing planner is reported as
    // a skipped test (or a failure, when DAGENTSC_BIN is set) rather than as no test at all.
    PlannerBinary.locate();

    // Only the planner writes the Service type; the Java renderer leaves it out.
    compile().andExpect(jsonPath("$.manifests[0].serviceYaml").value(containsString("type: ClusterIP")));
  }
}

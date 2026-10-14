package io.dagents.spring.core.compiler;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadComponent;
import io.dagents.spring.common.contracts.WorkloadEnvironmentVariable;
import io.dagents.spring.common.contracts.WorkloadManifest;
import io.dagents.spring.common.contracts.WorkloadPort;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.common.contracts.WorkloadResources;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * The gateway has two renderers: the dagentsc planner, and the Java renderer it falls back to.
 * Both run the same contract assertions. The planner tests also check that the planner's answer is
 * the one returned, because a gateway that always fell back once passed these tests.
 */
class ProcessManifestCompilerGatewayTest {
  private static final ManifestCompilerGateway FALLBACK_MUST_NOT_RUN = request -> {
    throw new AssertionError("the gateway fell back to the Java renderer; the planner's answer was not used");
  };

  @Test
  void returnsThePlannersAnswer() {
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(FALLBACK_MUST_NOT_RUN, PlannerBinary.locate().toString());

    WorkloadPlanResponse response = gateway.compile(request("request-plan"));

    assertMeetsTheSharedContract("request-plan", response);
    // Only the planner writes the Service type; the Java renderer leaves it out.
    assertTrue(response.manifests().get(0).serviceYaml().contains("type: ClusterIP"), response.combinedYaml());
  }

  @Test
  void usesTheJavaRendererWhenNoPlannerIsConfigured() {
    ProcessManifestCompilerGateway gateway = new ProcessManifestCompilerGateway(new JavaManifestCompiler(), null);

    WorkloadPlanResponse response = gateway.compile(request("java-plan"));

    assertMeetsTheSharedContract("java-plan", response);
    assertFalse(response.combinedYaml().contains("type: ClusterIP"), response.combinedYaml());
  }

  @Test
  void fallsBackWhenExternalCompilerFails() {
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), "/definitely/missing/dagentsc");

    WorkloadPlanResponse response = gateway.compile(request("fallback-plan"));

    assertMeetsTheSharedContract("fallback-plan", response);
    assertFalse(response.combinedYaml().contains("type: ClusterIP"), response.combinedYaml());
  }

  private static WorkloadCompileRequest request(String planId) {
    return new WorkloadCompileRequest(
        planId,
        "dagents",
        List.of(new WorkloadComponent(
            "compiler",
            "ghcr.io/example/compiler:latest",
            "Deployment",
            1,
            null,
            List.of(new WorkloadPort("http", 8080)),
            List.of(new WorkloadEnvironmentVariable("APP_ENV", "test")),
            List.of("--serve"),
            WorkloadResources.defaults()
        )),
        true,
        true
    );
  }

  /** What both renderers must produce for {@link #request}. */
  private static void assertMeetsTheSharedContract(String planId, WorkloadPlanResponse response) {
    assertEquals(planId, response.planId());
    assertEquals("dagents", response.namespace());
    assertEquals(1, response.manifests().size());

    WorkloadManifest manifest = response.manifests().get(0);
    assertEquals("compiler", manifest.componentName());
    assertEquals("Deployment", manifest.kind());
    assertTrue(manifest.deploymentYaml().contains("kind: Deployment"), manifest.deploymentYaml());
    assertTrue(manifest.deploymentYaml().contains("namespace: dagents"), manifest.deploymentYaml());
    assertTrue(manifest.deploymentYaml().contains("image: ghcr.io/example/compiler:latest"), manifest.deploymentYaml());
    assertTrue(manifest.deploymentYaml().contains("- \"--serve\""), manifest.deploymentYaml());
    assertTrue(manifest.deploymentYaml().contains("containerPort: 8080"), manifest.deploymentYaml());
    assertTrue(manifest.deploymentYaml().contains("value: \"test\""), manifest.deploymentYaml());
    assertTrue(manifest.serviceYaml().contains("kind: Service"), manifest.serviceYaml());
    assertTrue(manifest.serviceYaml().contains("targetPort: 8080"), manifest.serviceYaml());
    assertTrue(manifest.configMapYaml().contains("kind: ConfigMap"), manifest.configMapYaml());
    assertTrue(manifest.configMapYaml().contains("name: compiler-config"), manifest.configMapYaml());

    for (String yaml : List.of(manifest.deploymentYaml(), manifest.serviceYaml(), manifest.configMapYaml())) {
      assertTrue(response.combinedYaml().contains(yaml), response.combinedYaml());
    }
  }
}

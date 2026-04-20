package io.dagents.spring.core.compiler;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadComponent;
import io.dagents.spring.common.contracts.WorkloadEnvironmentVariable;
import io.dagents.spring.common.contracts.WorkloadPort;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.common.contracts.WorkloadResources;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

class ProcessManifestCompilerGatewayTest {
  @Test
  void consumesJsonFromOcamlCompilerProcess() {
    Path compiler = Path.of("..", "..", "..", "bindings", "ocaml", "_build", "default", "bin", "dagentsc.exe")
        .normalize()
        .toAbsolutePath();
    Assumptions.assumeTrue(Files.exists(compiler), "dagentsc binary must be built before running this test");

    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), compiler.toString());

    WorkloadPlanResponse response = gateway.compile(new WorkloadCompileRequest(
        "request-plan",
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
    ));

    assertEquals("request-plan", response.planId());
    assertEquals("dagents", response.namespace());
    assertEquals(1, response.manifests().size());
    assertTrue(response.combinedYaml().contains("kind: Deployment"));
    assertTrue(response.combinedYaml().contains("kind: Service"));
    assertTrue(response.combinedYaml().contains("kind: ConfigMap"));
  }

  @Test
  void fallsBackWhenExternalCompilerFails() {
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), "/definitely/missing/dagentsc");

    WorkloadPlanResponse response = gateway.compile(new WorkloadCompileRequest(
        "fallback-plan",
        "dagents",
        List.of(new WorkloadComponent(
            "compiler",
            "ghcr.io/example/compiler:latest",
            "Deployment",
            1,
            null,
            List.of(new WorkloadPort("http", 8080)),
            List.of(),
            List.of(),
            WorkloadResources.defaults()
        )),
        true,
        false
    ));

    assertEquals("fallback-plan", response.planId());
    assertTrue(response.combinedYaml().contains("kind: Deployment"));
  }
}

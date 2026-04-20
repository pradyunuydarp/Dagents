package io.dagents.spring.core.compiler;

import static org.junit.jupiter.api.Assertions.assertTrue;

import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadComponent;
import io.dagents.spring.common.contracts.WorkloadEnvironmentVariable;
import io.dagents.spring.common.contracts.WorkloadPort;
import io.dagents.spring.common.contracts.WorkloadResources;
import java.util.List;
import org.junit.jupiter.api.Test;

class JavaManifestCompilerTest {
  @Test
  void rendersDeploymentAndServiceYaml() {
    JavaManifestCompiler compiler = new JavaManifestCompiler();
    var response = compiler.compile(new WorkloadCompileRequest(
        "plan-1",
        "dagents",
        List.of(
            new WorkloadComponent(
                "spring-core",
                "ghcr.io/example/core:latest",
                "Deployment",
                2,
                null,
                List.of(new WorkloadPort("http", 8060)),
                List.of(new WorkloadEnvironmentVariable("APP_ENV", "cloud")),
                List.of("--server.port=8060"),
                WorkloadResources.defaults()
            ),
            new WorkloadComponent(
                "reconciler",
                "ghcr.io/example/reconciler:latest",
                "CronJob",
                1,
                "*/15 * * * *",
                List.of(),
                List.of(new WorkloadEnvironmentVariable("MODE", "reconcile")),
                List.of("--sync"),
                WorkloadResources.defaults()
            )
        ),
        true,
        true
    ));
    assertTrue(response.combinedYaml().contains("kind: Deployment"));
    assertTrue(response.combinedYaml().contains("kind: CronJob"));
    assertTrue(response.combinedYaml().contains("kind: Service"));
    assertTrue(response.combinedYaml().contains("kind: ConfigMap"));
    assertTrue(response.combinedYaml().contains("*/15 * * * *"));
    assertTrue(response.combinedYaml().contains("--sync"));
  }
}

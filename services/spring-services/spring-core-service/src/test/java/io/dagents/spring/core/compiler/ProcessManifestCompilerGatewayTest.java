package io.dagents.spring.core.compiler;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadComponent;
import io.dagents.spring.common.contracts.WorkloadEnvironmentVariable;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.common.contracts.WorkloadPort;
import io.dagents.spring.common.contracts.WorkloadResources;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class ProcessManifestCompilerGatewayTest {
  /** How long the test's own call to the planner may run. */
  private static final long PLANNER_SECONDS = 60;

  /** The dune build, used when {@code DAGENTSC_BIN} is not set. */
  private static final Path DUNE_PLANNER =
      Path.of("..", "..", "..", "bindings", "ocaml", "_build", "default", "bin", "dagentsc.exe")
          .normalize()
          .toAbsolutePath();

  /**
   * The planner: {@code DAGENTSC_BIN} when it is set, otherwise the dune build.
   *
   * <p>A {@code DAGENTSC_BIN} that names a file this process cannot run fails the test, because
   * the CI job sets it and a skip there would hide a missing planner. The test skips only when
   * {@code DAGENTSC_BIN} is not set and the dune build is missing.
   */
  private static Path planner() {
    String configured = System.getenv("DAGENTSC_BIN");
    if (configured != null && !configured.isBlank()) {
      Path path = Path.of(configured);
      assertTrue(
          Files.isRegularFile(path) && Files.isExecutable(path),
          "DAGENTSC_BIN names " + configured + ", which is not a file this process can run");
      return path;
    }
    Assumptions.assumeTrue(
        Files.isRegularFile(DUNE_PLANNER),
        "dagentsc binary is not available; build it or set DAGENTSC_BIN");
    return DUNE_PLANNER;
  }

  /**
   * What {@code dagentsc manifest compile} prints for the request, run here without the gateway.
   *
   * <p>The call has a time limit, so a planner that hangs fails the test instead of holding the
   * build until the CI job's limit.
   */
  private static JsonNode plannerAnswer(Path planner, WorkloadCompileRequest request, Path directory)
      throws Exception {
    ObjectMapper objectMapper = new ObjectMapper();
    Path input = directory.resolve("request.json");
    Path stdout = directory.resolve("stdout.json");
    Path stderr = directory.resolve("stderr.txt");
    Files.write(input, objectMapper.writeValueAsBytes(request));
    Process process = new ProcessBuilder(
        planner.toString(), "manifest", "compile", "--input", "-", "--output", "json")
        .redirectInput(input.toFile())
        .redirectOutput(stdout.toFile())
        .redirectError(stderr.toFile())
        .start();
    if (!process.waitFor(PLANNER_SECONDS, TimeUnit.SECONDS)) {
      process.destroyForcibly();
      fail("dagentsc did not finish within " + PLANNER_SECONDS + " s");
    }
    assertEquals(0, process.exitValue(), Files.readString(stderr, StandardCharsets.UTF_8));
    return objectMapper.readTree(stdout.toFile());
  }

  @Test
  void consumesJsonFromOcamlCompilerProcess(@TempDir Path directory) throws Exception {
    Path planner = planner();
    // planId and namespace are null, so the answer shows which renderer made it: the planner
    // uses dagents-plan and dagents, the Java renderer spring-workload-plan-<ms> and null.
    WorkloadCompileRequest request = new WorkloadCompileRequest(
        null,
        null,
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

    WorkloadPlanResponse response =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), planner.toString()).compile(request);
    JsonNode expected = plannerAnswer(planner, request, directory);

    assertEquals("dagents-plan", response.planId());
    assertEquals("dagents", response.namespace());
    assertEquals(expected.path("combinedYaml").asText(), response.combinedYaml());
    assertEquals(1, response.manifests().size());
    JsonNode manifest = expected.path("manifests").path(0);
    assertEquals(manifest.path("deploymentYaml").asText(), response.manifests().get(0).deploymentYaml());
    assertEquals(manifest.path("serviceYaml").asText(), response.manifests().get(0).serviceYaml());
    assertEquals(manifest.path("configMapYaml").asText(), response.manifests().get(0).configMapYaml());
  }

  @Test
  void fallsBackWhenExternalCompilerFails() {
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), "/definitely/missing/dagentsc");

    WorkloadPlanResponse response = gateway.compile(namedRequest());

    assertEquals("fallback-plan", response.planId());
    assertTrue(response.combinedYaml().contains("kind: Deployment"));
  }

  /** A request with a plan id, so the Java renderer's answer can be compared as a whole. */
  private static WorkloadCompileRequest namedRequest() {
    return new WorkloadCompileRequest(
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
    );
  }

  /** A stand-in planner: a shell script that reads all of its input, then runs {@code body}. */
  private static Path standInPlanner(Path directory, String body) throws Exception {
    Path script = directory.resolve("stand-in-planner");
    Files.writeString(script, "#!/bin/sh\ncat > /dev/null\n" + body + "\n", StandardCharsets.UTF_8);
    assertTrue(script.toFile().setExecutable(true), "could not make " + script + " executable");
    return script;
  }

  @Test
  void stopsAPlannerThatRunsPastTheTimeoutAndFallsBack(@TempDir Path directory) throws Exception {
    // The script starts a child and waits for it, as a wrapper around the planner would. The
    // child keeps the planner's output open, so only stopping both ends the call.
    Path childPid = directory.resolve("child.pid");
    Path planner = standInPlanner(directory, "sleep 60 &\necho $! > " + childPid + "\nwait");
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), planner.toString(), Duration.ofSeconds(1));
    WorkloadCompileRequest request = namedRequest();

    long started = System.nanoTime();
    WorkloadPlanResponse response = gateway.compile(request);
    Duration waited = Duration.ofNanos(System.nanoTime() - started);

    assertTrue(
        waited.compareTo(Duration.ofSeconds(20)) < 0,
        "the gateway waited " + waited + " for a planner that runs for 60 s, with a timeout of 1 s");
    assertEquals(new JavaManifestCompiler().compile(request), response);
    long pid = Long.parseLong(Files.readString(childPid, StandardCharsets.UTF_8).strip());
    long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
    while (isRunning(pid) && System.nanoTime() < deadline) {
      Thread.sleep(50);
    }
    assertFalse(isRunning(pid), "the planner's child process " + pid + " is still running");
  }

  @Test
  void fallsBackWhenThePlannerExitsButAProcessItLeftKeepsItsOutputOpen(@TempDir Path directory)
      throws Exception {
    // The script starts a child that keeps the planner's output open, and exits with status 0.
    // The gateway waits for the output to end until the timeout, then the Java renderer answers.
    // Once the script has exited, the gateway cannot find the child, so the test stops it.
    Path childPid = directory.resolve("child.pid");
    Path planner = standInPlanner(directory, "sleep 60 &\necho $! > " + childPid + "\nexit 0");
    ProcessManifestCompilerGateway gateway =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), planner.toString(), Duration.ofSeconds(1));
    WorkloadCompileRequest request = namedRequest();

    try {
      long started = System.nanoTime();
      WorkloadPlanResponse response = gateway.compile(request);
      Duration waited = Duration.ofNanos(System.nanoTime() - started);

      assertTrue(
          waited.compareTo(Duration.ofSeconds(20)) < 0,
          "the gateway waited " + waited + " for output that a child holds open, with a timeout of 1 s");
      assertEquals(new JavaManifestCompiler().compile(request), response);
    } finally {
      if (Files.exists(childPid)) {
        long pid = Long.parseLong(Files.readString(childPid, StandardCharsets.UTF_8).strip());
        ProcessHandle.of(pid).ifPresent(ProcessHandle::destroyForcibly);
      }
    }
  }

  private static boolean isRunning(long pid) {
    return ProcessHandle.of(pid).map(ProcessHandle::isAlive).orElse(false);
  }

  /** Planner output that exits with status 0 but is not a plan. */
  @ParameterizedTest
  @ValueSource(strings = {
      "{}",
      "[]",
      "\"a plan\"",
      "not json",
      "{\"planId\": null, \"namespace\": \"dagents\", \"manifests\": [], \"combinedYaml\": \"\"}",
      "{\"planId\": \"p\", \"namespace\": \"dagents\", \"manifests\": {}, \"combinedYaml\": \"\"}",
      "{\"planId\": \"p\", \"namespace\": \"dagents\", \"manifests\": [{}], \"combinedYaml\": \"\"}",
      "{\"planId\": \"p\", \"namespace\": \"dagents\", \"manifests\": []}"
  })
  void fallsBackWhenThePlannerPrintsSomethingThatIsNotAPlan(String output, @TempDir Path directory)
      throws Exception {
    Path printed = directory.resolve("output.txt");
    Files.writeString(printed, output, StandardCharsets.UTF_8);
    Path planner = standInPlanner(directory, "cat " + printed);
    WorkloadCompileRequest request = namedRequest();

    WorkloadPlanResponse response =
        new ProcessManifestCompilerGateway(new JavaManifestCompiler(), planner.toString()).compile(request);

    assertEquals(new JavaManifestCompiler().compile(request), response);
  }
}

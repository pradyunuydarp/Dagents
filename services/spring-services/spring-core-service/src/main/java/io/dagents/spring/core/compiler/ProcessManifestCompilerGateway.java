package io.dagents.spring.core.compiler;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadManifest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import java.io.BufferedWriter;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Renders a workload plan with the planner, {@code dagentsc manifest compile}, and falls back to
 * another renderer when it cannot.
 *
 * <p>The fallback answers when no planner command is set, when the planner cannot start, when it
 * runs longer than the timeout, when it exits with a status other than 0, and when its output is
 * not a plan. Each fallback after a planner call is logged as a warning with the reason, such as
 * the planner's error message. The response does not say which renderer made it.
 *
 * <p>A planner that runs past the timeout is stopped, together with the processes it started that
 * still run under it. A process that the planner leaves running when it exits is not stopped,
 * because Java finds a process's children through their parent, and that link ends when the
 * planner exits. {@code dagentsc} starts no other process.
 */
public class ProcessManifestCompilerGateway implements ManifestCompilerGateway {
  /**
   * How long one planner call may run: 10 seconds, the default of {@code DAGENTSC_TIMEOUT_SECONDS}
   * in the Python services' planner runner.
   */
  public static final Duration DEFAULT_TIMEOUT = Duration.ofSeconds(10);

  private static final Logger log = LoggerFactory.getLogger(ProcessManifestCompilerGateway.class);
  /** The most characters of the planner's error output that one log line quotes. */
  private static final int LOGGED_ERROR_CHARS = 2000;
  /**
   * Writes the planner's input and reads its two outputs, each on its own virtual thread, so a
   * planner that stops reading or writing cannot hold the request past the timeout.
   */
  private static final ExecutorService STREAMS = Executors.newVirtualThreadPerTaskExecutor();

  private final ManifestCompilerGateway fallback;
  private final String command;
  private final Duration timeout;
  private final ObjectMapper objectMapper = new ObjectMapper();

  public ProcessManifestCompilerGateway(ManifestCompilerGateway fallback, String command) {
    this(fallback, command, DEFAULT_TIMEOUT);
  }

  public ProcessManifestCompilerGateway(ManifestCompilerGateway fallback, String command, Duration timeout) {
    this.fallback = fallback;
    this.command = command;
    this.timeout = timeout;
    if (command == null || command.isBlank()) {
      log.info("DAGENTS_OCAML_MANIFEST_CLI is not set, so the Java renderer answers every compile");
    }
  }

  @Override
  public WorkloadPlanResponse compile(WorkloadCompileRequest request) {
    if (command == null || command.isBlank()) {
      return fallback.compile(request);
    }
    long deadline = System.nanoTime() + timeout.toNanos();
    Process process = null;
    try {
      process = new ProcessBuilder(command, "manifest", "compile", "--input", "-", "--output", "json").start();
      Process planner = process;
      Future<byte[]> stdout = STREAMS.submit(() -> planner.getInputStream().readAllBytes());
      Future<byte[]> stderr = STREAMS.submit(() -> planner.getErrorStream().readAllBytes());
      Future<Void> input = STREAMS.submit(() -> {
        // writeValue closes the writer, which ends the planner's input. Nothing may write to or
        // flush the writer after that: a flush() here once threw on the closed stream, and the
        // planner's answer was thrown away for every request.
        try (BufferedWriter writer =
            new BufferedWriter(new OutputStreamWriter(planner.getOutputStream(), StandardCharsets.UTF_8))) {
          objectMapper.writeValue(writer, request);
        }
        return null;
      });
      if (!process.waitFor(remaining(deadline), TimeUnit.NANOSECONDS)) {
        log.warn(
            "The planner {} did not finish within {} ms, so it was stopped and the Java renderer answered instead",
            command, timeout.toMillis());
        return fallback.compile(request);
      }
      int exitCode = process.exitValue();
      byte[] out = stdout.get(remaining(deadline), TimeUnit.NANOSECONDS);
      byte[] err = stderr.get(remaining(deadline), TimeUnit.NANOSECONDS);
      if (exitCode != 0 || out.length == 0) {
        String message = new String(err, StandardCharsets.UTF_8).strip();
        if (message.length() > LOGGED_ERROR_CHARS) {
          message = message.substring(0, LOGGED_ERROR_CHARS) + "...";
        }
        log.warn(
            "The planner {} exited with status {} and printed {} bytes, so the Java renderer answered instead. "
                + "The planner said: {}",
            command, exitCode, out.length, message.isEmpty() ? "nothing" : message);
        return fallback.compile(request);
      }
      // The planner exited with 0, so it read all of its input. A failed write still means it
      // answered a request it did not fully receive.
      input.get(remaining(deadline), TimeUnit.NANOSECONDS);
      JsonNode payload = objectMapper.readTree(out);
      String problem = notAPlan(payload);
      if (problem != null) {
        log.warn(
            "The planner {} exited with status 0 but printed something that is not a plan ({}), "
                + "so the Java renderer answered instead",
            command, problem);
        return fallback.compile(request);
      }
      List<WorkloadManifest> manifests = new ArrayList<>();
      for (JsonNode manifest : payload.path("manifests")) {
        manifests.add(new WorkloadManifest(
            manifest.path("componentName").asText(),
            manifest.path("kind").asText(),
            manifest.path("deploymentYaml").asText(),
            manifest.path("serviceYaml").isNull() ? null : manifest.path("serviceYaml").asText(),
            manifest.path("configMapYaml").isNull() ? null : manifest.path("configMapYaml").asText()
        ));
      }
      String namespace = payload.path("namespace").asText(request.namespace());
      return new WorkloadPlanResponse(
          payload.path("planId").asText(), namespace, manifests, payload.path("combinedYaml").asText());
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      log.warn("The call to the planner {} was interrupted, so the Java renderer answered instead", command);
      return fallback.compile(request);
    } catch (TimeoutException error) {
      log.warn(
          "The planner {} exited, but its output did not end within {} ms, so the Java renderer answered instead",
          command, timeout.toMillis());
      return fallback.compile(request);
    } catch (Exception error) {
      log.warn("The planner {} could not be used ({}), so the Java renderer answered instead", command, error.toString());
      return fallback.compile(request);
    } finally {
      if (process != null) {
        // A wrapper script's children keep the output open after the script is stopped, so stop
        // them first, while they can still be found from the script.
        process.descendants().forEach(ProcessHandle::destroyForcibly);
        if (process.isAlive()) {
          process.destroyForcibly();
        }
      }
    }
  }

  private static long remaining(long deadline) {
    return Math.max(0, deadline - System.nanoTime());
  }

  /** Why the planner's output is not a plan, or {@code null} when it is one. */
  private static String notAPlan(JsonNode payload) {
    if (!payload.isObject()) {
      return "not a JSON object";
    }
    if (!payload.path("planId").isTextual()) {
      return "planId is not a text";
    }
    if (!payload.path("combinedYaml").isTextual()) {
      return "combinedYaml is not a text";
    }
    if (!payload.path("manifests").isArray()) {
      return "manifests is not a list";
    }
    for (JsonNode manifest : payload.path("manifests")) {
      if (!manifest.path("componentName").isTextual() || !manifest.path("deploymentYaml").isTextual()) {
        return "a manifest has no componentName or deploymentYaml";
      }
    }
    return null;
  }
}

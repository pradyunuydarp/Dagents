package io.dagents.spring.core.compiler;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadManifest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import java.io.BufferedWriter;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

public class ProcessManifestCompilerGateway implements ManifestCompilerGateway {
  private final ManifestCompilerGateway fallback;
  private final String command;
  private final ObjectMapper objectMapper = new ObjectMapper();

  public ProcessManifestCompilerGateway(ManifestCompilerGateway fallback, String command) {
    this.fallback = fallback;
    this.command = command;
  }

  @Override
  public WorkloadPlanResponse compile(WorkloadCompileRequest request) {
    if (command == null || command.isBlank()) {
      return fallback.compile(request);
    }
    try {
      Process process = new ProcessBuilder(command, "manifest", "compile", "--input", "-", "--output", "json").start();
      try (BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(process.getOutputStream(), StandardCharsets.UTF_8))) {
        objectMapper.writeValue(writer, request);
        writer.flush();
        writer.close();
      }
      byte[] stdout = process.getInputStream().readAllBytes();
      byte[] stderr = process.getErrorStream().readAllBytes();
      int exitCode = process.waitFor();
      if (exitCode == 0 && stdout.length > 0) {
        JsonNode payload = objectMapper.readTree(stdout);
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
        String planId = payload.path("planId").asText(request.planId());
        String namespace = payload.path("namespace").asText(request.namespace());
        return new WorkloadPlanResponse(planId, namespace, manifests, payload.path("combinedYaml").asText());
      }
    } catch (Exception ignored) {
      return fallback.compile(request);
    }
    return fallback.compile(request);
  }
}

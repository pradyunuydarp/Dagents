package io.dagents.spring.core.compiler;

import io.dagents.spring.common.contracts.WorkloadCompileRequest;
import io.dagents.spring.common.contracts.WorkloadComponent;
import io.dagents.spring.common.contracts.WorkloadManifest;
import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import io.dagents.spring.common.contracts.WorkloadResources;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

public class JavaManifestCompiler implements ManifestCompilerGateway {
  @Override
  public WorkloadPlanResponse compile(WorkloadCompileRequest request) {
    String planId = request.planId() == null || request.planId().isBlank()
        ? "spring-workload-plan-" + Instant.now().toEpochMilli()
        : request.planId();
    List<WorkloadManifest> manifests = new ArrayList<>();
    List<String> rendered = new ArrayList<>();
    List<WorkloadComponent> components = request.components() == null ? List.of() : request.components();
    for (WorkloadComponent component : components) {
      String deploymentYaml = renderWorkload(request.namespace(), component);
      String serviceYaml =
          request.includeServices() && !"Service".equals(normalizeKind(component))
              ? renderService(request.namespace(), component)
              : null;
      String configMapYaml =
          request.includeConfigMaps() && !"ConfigMap".equals(normalizeKind(component))
              ? renderConfigMap(request.namespace(), component)
              : null;
      manifests.add(new WorkloadManifest(component.name(), component.kind(), deploymentYaml, serviceYaml, configMapYaml));
      if (deploymentYaml != null && !deploymentYaml.isBlank()) {
        rendered.add(deploymentYaml);
      }
      if (serviceYaml != null && !serviceYaml.isBlank()) {
        rendered.add(serviceYaml);
      }
      if (configMapYaml != null && !configMapYaml.isBlank()) {
        rendered.add(configMapYaml);
      }
    }
    return new WorkloadPlanResponse(planId, request.namespace(), manifests, String.join("\n---\n", rendered));
  }

  private String renderWorkload(String namespace, WorkloadComponent component) {
    WorkloadResources resources = component.resources() == null ? WorkloadResources.defaults() : component.resources();
    List<String> args = component.args() == null ? List.of() : component.args();
    List<String> envLines = component.env() == null ? List.of() : component.env().stream()
        .flatMap(variable -> List.of("        - name: " + variable.name(), "          value: \"" + variable.value() + "\"").stream())
        .toList();
    List<String> portsLines = component.ports() == null ? List.of() : component.ports().stream()
        .flatMap(port -> List.of("        - name: " + port.name(), "          containerPort: " + port.containerPort()).stream())
        .toList();
    String kind = normalizeKind(component);
    if ("Job".equals(kind)) {
      return String.join("\n",
          "apiVersion: batch/v1",
          "kind: Job",
          "metadata:",
          "  name: " + component.name(),
          "  namespace: " + namespace,
          "spec:",
          "  template:",
          "    spec:",
          "      restartPolicy: Never",
          "      containers:",
          "      - name: main",
          "        image: " + component.image(),
          argsBlock(args),
          envBlock(envLines),
          resourcesBlock(resources));
    }
    if ("CronJob".equals(kind)) {
      return String.join("\n",
          "apiVersion: batch/v1",
          "kind: CronJob",
          "metadata:",
          "  name: " + component.name(),
          "  namespace: " + namespace,
          "spec:",
          "  schedule: \"" + (component.schedule() == null ? "0 * * * *" : component.schedule()) + "\"",
          "  jobTemplate:",
          "    spec:",
          "      template:",
          "        spec:",
          "          restartPolicy: Never",
          "          containers:",
          "          - name: main",
          "            image: " + component.image(),
          indent("          ", argsBlock(args)),
          indent("          ", portsBlock(portsLines)),
          indent("          ", envBlock(envLines)),
          indent("          ", resourcesBlock(resources)));
    }
    if ("Service".equals(kind)) {
      return renderService(namespace, component);
    }
    if ("ConfigMap".equals(kind)) {
      return renderConfigMap(namespace, component);
    }
    return String.join("\n",
        "apiVersion: apps/v1",
        "kind: Deployment",
        "metadata:",
        "  name: " + component.name(),
        "  namespace: " + namespace,
        "spec:",
        "  replicas: " + Math.max(component.replicas(), 1),
        "  selector:",
        "    matchLabels:",
        "      app: " + component.name(),
        "  template:",
        "    metadata:",
        "      labels:",
        "        app: " + component.name(),
        "    spec:",
        "      containers:",
        "      - name: main",
        "        image: " + component.image(),
        argsBlock(args),
        portsBlock(portsLines),
        envBlock(envLines),
        resourcesBlock(resources));
  }

  private String renderService(String namespace, WorkloadComponent component) {
    if (component.ports() == null || component.ports().isEmpty()) {
      return "";
    }
    List<String> portLines = component.ports().stream()
        .flatMap(port -> List.of("  - name: " + port.name(), "    port: " + port.containerPort(), "    targetPort: " + port.containerPort()).stream())
        .toList();
    List<String> lines = new ArrayList<>();
    lines.add("apiVersion: v1");
    lines.add("kind: Service");
    lines.add("metadata:");
    lines.add("  name: " + component.name());
    lines.add("  namespace: " + namespace);
    lines.add("spec:");
    lines.add("  selector:");
    lines.add("    app: " + component.name());
    lines.add("  ports:");
    lines.addAll(portLines);
    return String.join("\n", lines);
  }

  private String renderConfigMap(String namespace, WorkloadComponent component) {
    String kind = normalizeKind(component);
    return String.join("\n",
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: " + component.name() + "-config",
        "  namespace: " + namespace,
        "data:",
        "  component-kind: \"" + kind + "\"",
        "  image: \"" + component.image() + "\"");
  }

  private String argsBlock(List<String> args) {
    if (args.isEmpty()) {
      return "";
    }
    List<String> lines = new ArrayList<>();
    lines.add("        args:");
    args.forEach(arg -> lines.add("        - \"" + arg + "\""));
    return String.join("\n", lines);
  }

  private String envBlock(List<String> envLines) {
    if (envLines.isEmpty()) {
      return "";
    }
    List<String> lines = new ArrayList<>();
    lines.add("        env:");
    lines.addAll(envLines);
    return String.join("\n", lines);
  }

  private String portsBlock(List<String> portLines) {
    if (portLines.isEmpty()) {
      return "";
    }
    List<String> lines = new ArrayList<>();
    lines.add("        ports:");
    lines.addAll(portLines);
    return String.join("\n", lines);
  }

  private String resourcesBlock(WorkloadResources resources) {
    return String.join("\n",
        "        resources:",
        "          requests:",
        "            cpu: " + resources.cpuRequest(),
        "            memory: " + resources.memoryRequest(),
        "          limits:",
        "            cpu: " + resources.cpuLimit(),
        "            memory: " + resources.memoryLimit());
  }

  private String normalizeKind(WorkloadComponent component) {
    return component.kind() == null || component.kind().isBlank() ? "Deployment" : component.kind();
  }

  private String indent(String prefix, String block) {
    if (block == null || block.isBlank()) {
      return "";
    }
    return block.lines().map(line -> prefix + line).reduce((left, right) -> left + "\n" + right).orElse("");
  }
}

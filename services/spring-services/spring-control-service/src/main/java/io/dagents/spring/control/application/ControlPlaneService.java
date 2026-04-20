package io.dagents.spring.control.application;

import io.dagents.spring.common.contracts.AgentRegistrationRequest;
import io.dagents.spring.common.contracts.AgentSnapshot;
import io.dagents.spring.common.contracts.DeploymentPlan;
import io.dagents.spring.common.contracts.DeploymentSyncRequest;
import io.dagents.spring.common.contracts.DeploymentSyncResponse;
import io.dagents.spring.common.contracts.DesiredDeploymentRequest;
import io.dagents.spring.common.contracts.HeartbeatRequest;
import io.dagents.spring.common.contracts.HeartbeatResponse;
import io.dagents.spring.common.contracts.RegisterResponse;
import io.dagents.spring.common.contracts.TelemetryAck;
import io.dagents.spring.common.contracts.TelemetryEnvelope;
import io.dagents.spring.control.infrastructure.InMemoryAgentRegistryRepository;
import io.dagents.spring.control.infrastructure.InMemoryDeploymentRepository;
import io.dagents.spring.control.infrastructure.InMemoryTelemetryRepository;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Service;

@Service
public class ControlPlaneService {
  private final InMemoryAgentRegistryRepository agents;
  private final InMemoryDeploymentRepository deployments;
  private final InMemoryTelemetryRepository telemetry;

  public ControlPlaneService(
      InMemoryAgentRegistryRepository agents,
      InMemoryDeploymentRepository deployments,
      InMemoryTelemetryRepository telemetry
  ) {
    this.agents = agents;
    this.deployments = deployments;
    this.telemetry = telemetry;
  }

  public RegisterResponse register(AgentRegistrationRequest request) {
    AgentSnapshot current = agents.find(request.agent().agentId());
    DeploymentPlan deployment = deployments.find(request.agent().agentId());
    agents.save(new AgentSnapshot(
        request.agent(),
        request.scope(),
        request.version(),
        request.capabilities(),
        current == null ? "REGISTERED" : current.status(),
        current == null ? null : current.lastSeenAt(),
        current == null ? null : current.lastHeartbeatStatus(),
        current == null ? null : current.lastIngestionId(),
        current == null ? null : current.desiredBundleId(),
        current == null ? null : current.desiredBundleVersion(),
        current == null ? null : current.currentBundleId(),
        current == null ? null : current.currentBundleVersion()
    ));
    return new RegisterResponse(true, deployment == null ? null : deployment.planToken(), request.capabilities());
  }

  public HeartbeatResponse heartbeat(HeartbeatRequest request) {
    AgentSnapshot current = Optional.ofNullable(agents.find(request.agent().agentId()))
        .orElse(new AgentSnapshot(request.agent(), java.util.Map.of(), "unknown", List.of(), "REGISTERED", null, null, null, null, null, null, null));
    AgentSnapshot updated = new AgentSnapshot(
        current.agent(),
        current.scope(),
        current.version(),
        current.capabilities(),
        request.status(),
        request.timestamp(),
        request.status(),
        current.lastIngestionId(),
        current.desiredBundleId(),
        current.desiredBundleVersion(),
        current.currentBundleId(),
        current.currentBundleVersion()
    );
    agents.save(updated);
    boolean syncRequired = updated.desiredBundleId() != null && (!updated.desiredBundleId().equals(updated.currentBundleId())
        || !java.util.Objects.equals(updated.desiredBundleVersion(), updated.currentBundleVersion()));
    return new HeartbeatResponse(true, syncRequired ? "SYNC_REQUIRED" : "ACTIVE");
  }

  public TelemetryAck ingestTelemetry(TelemetryEnvelope envelope) {
    String ingestionId = telemetry.append(envelope);
    AgentSnapshot current = agents.find(envelope.agent().agentId());
    if (current != null) {
      agents.save(new AgentSnapshot(
          current.agent(),
          current.scope(),
          current.version(),
          current.capabilities(),
          current.status(),
          current.lastSeenAt(),
          current.lastHeartbeatStatus(),
          ingestionId,
          current.desiredBundleId(),
          current.desiredBundleVersion(),
          current.currentBundleId(),
          current.currentBundleVersion()
      ));
    }
    return new TelemetryAck(true, ingestionId);
  }

  public DeploymentPlan planDeployment(DesiredDeploymentRequest request) {
    String digest = digest(request.agentId() + ":" + request.bundleId() + ":" + request.bundleVersion() + ":" + request.bundleUri() + ":" + request.config());
    DeploymentPlan plan = new DeploymentPlan(
        request.agentId(),
        request.bundleId(),
        request.bundleVersion(),
        request.bundleUri(),
        request.config(),
        "plan-" + digest.substring(0, 12),
        digest,
        Instant.now().getEpochSecond()
    );
    deployments.save(plan);
    AgentSnapshot current = agents.find(request.agentId());
    if (current != null) {
      agents.save(new AgentSnapshot(
          current.agent(),
          current.scope(),
          current.version(),
          current.capabilities(),
          current.status(),
          current.lastSeenAt(),
          current.lastHeartbeatStatus(),
          current.lastIngestionId(),
          request.bundleId(),
          request.bundleVersion(),
          current.currentBundleId(),
          current.currentBundleVersion()
      ));
    }
    return plan;
  }

  public DeploymentSyncResponse syncDeployment(DeploymentSyncRequest request) {
    DeploymentPlan plan = deployments.find(request.agent().agentId());
    AgentSnapshot current = Optional.ofNullable(agents.find(request.agent().agentId()))
        .orElse(new AgentSnapshot(request.agent(), java.util.Map.of(), "unknown", List.of(), "REGISTERED", null, null, null, null, null, null, null));
    agents.save(new AgentSnapshot(
        current.agent(),
        current.scope(),
        current.version(),
        current.capabilities(),
        current.status(),
        current.lastSeenAt(),
        current.lastHeartbeatStatus(),
        current.lastIngestionId(),
        current.desiredBundleId(),
        current.desiredBundleVersion(),
        request.bundleId(),
        request.bundleVersion()
    ));
    if (plan == null) {
      return new DeploymentSyncResponse(true, "", "", null, null, null);
    }
    boolean upToDate = plan.bundleId().equals(request.bundleId()) && plan.bundleVersion().equals(request.bundleVersion());
    return new DeploymentSyncResponse(
        upToDate,
        plan.planToken(),
        plan.configDigest(),
        plan.bundleId(),
        plan.bundleVersion(),
        plan.bundleUri()
    );
  }

  public List<AgentSnapshot> listAgents() {
    return agents.findAll();
  }

  public AgentSnapshot getAgent(String agentId) {
    return agents.find(agentId);
  }

  private static String digest(String payload) {
    try {
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      return HexFormat.of().formatHex(md.digest(payload.getBytes(StandardCharsets.UTF_8)));
    } catch (Exception ex) {
      throw new IllegalStateException("Unable to generate deployment digest", ex);
    }
  }
}

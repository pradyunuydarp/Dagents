package io.dagents.spring.control.application;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.dagents.spring.common.contracts.AgentIdentity;
import io.dagents.spring.common.contracts.AgentRegistrationRequest;
import io.dagents.spring.common.contracts.DeploymentSyncRequest;
import io.dagents.spring.common.contracts.DesiredDeploymentRequest;
import io.dagents.spring.common.contracts.HeartbeatRequest;
import io.dagents.spring.common.contracts.TelemetryEnvelope;
import io.dagents.spring.control.infrastructure.InMemoryAgentRegistryRepository;
import io.dagents.spring.control.infrastructure.InMemoryDeploymentRepository;
import io.dagents.spring.control.infrastructure.InMemoryTelemetryRepository;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class ControlPlaneServiceTest {
  private ControlPlaneService service;
  private AgentIdentity agent;

  @BeforeEach
  void setUp() {
    service = new ControlPlaneService(
        new InMemoryAgentRegistryRepository(),
        new InMemoryDeploymentRepository(),
        new InMemoryTelemetryRepository()
    );
    agent = new AgentIdentity("lma-1", "alpha", "local-agent", "LMA");
  }

  @Test
  void tracksRegistrationDeploymentAndSync() {
    var registration = service.register(new AgentRegistrationRequest(agent, Map.of("tenant", "alpha"), "0.1.0", List.of("monitoring")));
    assertTrue(registration.accepted());

    var plan = service.planDeployment(new DesiredDeploymentRequest(agent.agentId(), "bundle-a", "1.2.0", "s3://bundles/a", Map.of("tenant", "alpha")));
    assertEquals("bundle-a", plan.bundleId());

    var heartbeat = service.heartbeat(new HeartbeatRequest(agent, "ACTIVE", 100L));
    assertEquals("SYNC_REQUIRED", heartbeat.desiredState());

    var sync = service.syncDeployment(new DeploymentSyncRequest(agent, "bundle-a", "1.0.0"));
    assertFalse(sync.upToDate());
    assertEquals("1.2.0", sync.desiredBundleVersion());

    var ack = service.ingestTelemetry(new TelemetryEnvelope(agent, List.of(), Map.of()));
    assertTrue(ack.ack());
  }
}

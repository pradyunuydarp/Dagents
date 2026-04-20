package io.dagents.spring.control.api;

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
import io.dagents.spring.control.application.ControlPlaneService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/agents")
public class AgentControlController {
  private final ControlPlaneService controlPlaneService;

  public AgentControlController(ControlPlaneService controlPlaneService) {
    this.controlPlaneService = controlPlaneService;
  }

  @PutMapping("/{agentId}/registration")
  public RegisterResponse register(@PathVariable String agentId, @Valid @RequestBody AgentRegistrationRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.register(request);
  }

  @PostMapping("/{agentId}/heartbeats")
  public HeartbeatResponse heartbeat(@PathVariable String agentId, @Valid @RequestBody HeartbeatRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.heartbeat(request);
  }

  @PostMapping("/{agentId}/telemetry")
  public TelemetryAck telemetry(@PathVariable String agentId, @Valid @RequestBody TelemetryEnvelope request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.ingestTelemetry(request);
  }

  @PutMapping("/{agentId}/desired-deployment")
  public DeploymentPlan desiredDeployment(@PathVariable String agentId, @RequestBody DesiredDeploymentRequest request) {
    if (!agentId.equals(request.agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agentId");
    }
    return controlPlaneService.planDeployment(request);
  }

  @PostMapping("/{agentId}/deployment-sync")
  public DeploymentSyncResponse deploymentSync(@PathVariable String agentId, @RequestBody DeploymentSyncRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.syncDeployment(request);
  }

  @GetMapping
  public List<AgentSnapshot> agents() {
    return controlPlaneService.listAgents();
  }

  @GetMapping("/{agentId}")
  public AgentSnapshot agent(@PathVariable String agentId) {
    return controlPlaneService.getAgent(agentId);
  }
}

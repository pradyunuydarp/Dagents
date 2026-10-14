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

  /**
   * Register an agent, or update its registration.
   *
   * <p>A new agent is stored with status {@code REGISTERED}. Registering again replaces the agent's
   * identity, scope, version and capabilities and keeps the rest of what is known about it.
   *
   * @param agentId the agent's id; must equal {@code agent.agentId} in the body
   * @param request the registration
   */
  @PutMapping("/{agentId}/registration")
  public RegisterResponse register(@PathVariable String agentId, @Valid @RequestBody AgentRegistrationRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.register(request);
  }

  /**
   * Record a heartbeat and say whether the agent must change its bundle.
   *
   * <p>An agent that never registered is stored by its first heartbeat, with version
   * {@code unknown}.
   *
   * @param agentId the agent's id; must equal {@code agent.agentId} in the body
   * @param request the heartbeat
   */
  @PostMapping("/{agentId}/heartbeats")
  public HeartbeatResponse heartbeat(@PathVariable String agentId, @Valid @RequestBody HeartbeatRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.heartbeat(request);
  }

  /**
   * Store a batch of metrics from an agent.
   *
   * <p>The batch is kept in memory and is not returned by any endpoint. A registered agent's
   * {@code lastIngestionId} is updated.
   *
   * @param agentId the agent's id; must equal {@code agent.agentId} in the body
   * @param request the metrics
   */
  @PostMapping("/{agentId}/telemetry")
  public TelemetryAck telemetry(@PathVariable String agentId, @Valid @RequestBody TelemetryEnvelope request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.ingestTelemetry(request);
  }

  /**
   * Set the bundle an agent should run.
   *
   * <p>The plan replaces any earlier plan for the agent. A registered agent's desired bundle is
   * updated.
   *
   * @param agentId the agent's id; must equal {@code agentId} in the body
   * @param request the bundle and its settings
   */
  @PutMapping("/{agentId}/desired-deployment")
  public DeploymentPlan desiredDeployment(@PathVariable String agentId, @RequestBody DesiredDeploymentRequest request) {
    if (!agentId.equals(request.agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agentId");
    }
    return controlPlaneService.planDeployment(request);
  }

  /**
   * Record the bundle an agent runs, and compare it with the desired one.
   *
   * <p>An agent that never registered is stored by its first report, with version {@code unknown}.
   *
   * @param agentId the agent's id; must equal {@code agent.agentId} in the body
   * @param request the bundle the agent runs
   */
  @PostMapping("/{agentId}/deployment-sync")
  public DeploymentSyncResponse deploymentSync(@PathVariable String agentId, @RequestBody DeploymentSyncRequest request) {
    if (!agentId.equals(request.agent().agentId())) {
      throw new IllegalArgumentException("Path agentId must match body agent.agentId");
    }
    return controlPlaneService.syncDeployment(request);
  }

  /**
   * List every known agent, sorted by agent id.
   */
  @GetMapping
  public List<AgentSnapshot> agents() {
    return controlPlaneService.listAgents();
  }

  /**
   * Fetch what is known about one agent.
   *
   * <p>An unknown id answers 200 with an empty body.
   *
   * @param agentId the agent's id
   */
  @GetMapping("/{agentId}")
  public AgentSnapshot agent(@PathVariable String agentId) {
    return controlPlaneService.getAgent(agentId);
  }
}

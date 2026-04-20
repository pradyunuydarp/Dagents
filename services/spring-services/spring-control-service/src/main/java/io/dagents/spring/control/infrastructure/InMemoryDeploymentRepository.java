package io.dagents.spring.control.infrastructure;

import io.dagents.spring.common.contracts.DeploymentPlan;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Repository;

@Repository
public class InMemoryDeploymentRepository {
  private final Map<String, DeploymentPlan> plans = new ConcurrentHashMap<>();

  public DeploymentPlan save(DeploymentPlan plan) {
    plans.put(plan.agentId(), plan);
    return plan;
  }

  public DeploymentPlan find(String agentId) {
    return plans.get(agentId);
  }
}

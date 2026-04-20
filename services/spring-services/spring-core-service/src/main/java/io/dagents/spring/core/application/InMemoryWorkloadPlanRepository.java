package io.dagents.spring.core.application;

import io.dagents.spring.common.contracts.WorkloadPlanResponse;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Repository;

@Repository
public class InMemoryWorkloadPlanRepository {
  private final Map<String, WorkloadPlanResponse> plans = new ConcurrentHashMap<>();

  public WorkloadPlanResponse save(WorkloadPlanResponse plan) {
    plans.put(plan.planId(), plan);
    return plan;
  }

  public WorkloadPlanResponse find(String planId) {
    return plans.get(planId);
  }
}

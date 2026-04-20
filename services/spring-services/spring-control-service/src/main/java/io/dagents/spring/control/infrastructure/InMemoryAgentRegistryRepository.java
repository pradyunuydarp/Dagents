package io.dagents.spring.control.infrastructure;

import io.dagents.spring.common.contracts.AgentSnapshot;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Repository;

@Repository
public class InMemoryAgentRegistryRepository {
  private final Map<String, AgentSnapshot> snapshots = new ConcurrentHashMap<>();

  public AgentSnapshot save(AgentSnapshot snapshot) {
    snapshots.put(snapshot.agent().agentId(), snapshot);
    return snapshot;
  }

  public AgentSnapshot find(String agentId) {
    return snapshots.get(agentId);
  }

  public List<AgentSnapshot> findAll() {
    List<AgentSnapshot> results = new ArrayList<>(snapshots.values());
    results.sort(Comparator.comparing(snapshot -> snapshot.agent().agentId()));
    return results;
  }
}

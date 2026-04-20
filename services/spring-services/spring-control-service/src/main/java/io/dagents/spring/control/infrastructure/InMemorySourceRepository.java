package io.dagents.spring.control.infrastructure;

import io.dagents.spring.common.contracts.SourceSpec;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Repository;

@Repository
public class InMemorySourceRepository {
  private final Map<String, SourceSpec> sources = new ConcurrentHashMap<>();

  public SourceSpec save(SourceSpec sourceSpec) {
    sources.put(sourceSpec.sourceId(), sourceSpec);
    return sourceSpec;
  }

  public SourceSpec find(String sourceId) {
    return sources.get(sourceId);
  }

  public List<SourceSpec> findAll() {
    List<SourceSpec> results = new ArrayList<>(sources.values());
    results.sort(Comparator.comparing(SourceSpec::sourceId));
    return results;
  }
}

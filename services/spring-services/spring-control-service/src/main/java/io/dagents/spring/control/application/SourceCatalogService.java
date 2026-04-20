package io.dagents.spring.control.application;

import io.dagents.spring.common.contracts.SourceSpec;
import io.dagents.spring.common.contracts.SourceValidationResult;
import io.dagents.spring.control.infrastructure.InMemorySourceRepository;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class SourceCatalogService {
  private final InMemorySourceRepository sources;

  public SourceCatalogService(InMemorySourceRepository sources) {
    this.sources = sources;
  }

  public SourceSpec register(SourceSpec sourceSpec) {
    return sources.save(sourceSpec);
  }

  public SourceSpec get(String sourceId) {
    return sources.find(sourceId);
  }

  public List<SourceSpec> list() {
    return sources.findAll();
  }

  public SourceValidationResult validate(String sourceId) {
    SourceSpec sourceSpec = sources.find(sourceId);
    if (sourceSpec == null) {
      throw new IllegalArgumentException("Unknown source: " + sourceId);
    }
    boolean valid = sourceSpec.connectionRef() != null || "inline".equals(sourceSpec.kind());
    return new SourceValidationResult(valid, valid ? List.of() : List.of("connectionRef is required for non-inline sources"), List.of());
  }
}

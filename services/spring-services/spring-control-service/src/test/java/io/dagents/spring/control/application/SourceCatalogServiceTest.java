package io.dagents.spring.control.application;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.dagents.spring.common.contracts.SourceBatching;
import io.dagents.spring.common.contracts.SourceSpec;
import io.dagents.spring.control.infrastructure.InMemorySourceRepository;
import java.util.Map;
import org.junit.jupiter.api.Test;

class SourceCatalogServiceTest {
  @Test
  void validatesInlineSourceWithoutConnection() {
    SourceCatalogService service = new SourceCatalogService(new InMemorySourceRepository());
    service.register(new SourceSpec("inline-source", "inline", null, Map.of("records", java.util.List.of()), "rows", Map.of(), new SourceBatching(1000, null), Map.of(), Map.of()));
    assertTrue(service.validate("inline-source").valid());
  }

  @Test
  void rejectsMissingConnectionForPostgresSource() {
    SourceCatalogService service = new SourceCatalogService(new InMemorySourceRepository());
    service.register(new SourceSpec("pg-source", "postgres", null, Map.of("table", "events"), "rows", Map.of(), new SourceBatching(1000, null), Map.of(), Map.of()));
    assertFalse(service.validate("pg-source").valid());
  }
}

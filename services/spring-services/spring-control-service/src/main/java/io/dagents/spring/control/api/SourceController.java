package io.dagents.spring.control.api;

import io.dagents.spring.common.contracts.SourceSpec;
import io.dagents.spring.common.contracts.SourceValidationResult;
import io.dagents.spring.control.application.SourceCatalogService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/sources")
public class SourceController {
  private final SourceCatalogService sourceCatalogService;

  public SourceController(SourceCatalogService sourceCatalogService) {
    this.sourceCatalogService = sourceCatalogService;
  }

  @PostMapping
  public SourceSpec register(@Valid @RequestBody SourceSpec request) {
    return sourceCatalogService.register(request);
  }

  @GetMapping
  public List<SourceSpec> list() {
    return sourceCatalogService.list();
  }

  @GetMapping("/{sourceId}")
  public SourceSpec get(@PathVariable String sourceId) {
    return sourceCatalogService.get(sourceId);
  }

  @PostMapping("/{sourceId}:validate")
  public SourceValidationResult validate(@PathVariable String sourceId) {
    return sourceCatalogService.validate(sourceId);
  }
}

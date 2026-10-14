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

  /**
   * Store a source definition in the catalog.
   *
   * <p>A definition with the same {@code sourceId} is replaced. Nothing is read from the source.
   *
   * @param request the source definition
   */
  @PostMapping
  public SourceSpec register(@Valid @RequestBody SourceSpec request) {
    return sourceCatalogService.register(request);
  }

  /**
   * List every stored source definition, sorted by source id.
   */
  @GetMapping
  public List<SourceSpec> list() {
    return sourceCatalogService.list();
  }

  /**
   * Fetch one stored source definition.
   *
   * <p>An unknown id answers 200 with an empty body.
   *
   * @param sourceId the {@code sourceId} the source was registered with
   */
  @GetMapping("/{sourceId}")
  public SourceSpec get(@PathVariable String sourceId) {
    return sourceCatalogService.get(sourceId);
  }

  /**
   * Check that a stored source definition names a connection.
   *
   * <p>Only {@code kind} and {@code connectionRef} are checked; no connection is opened.
   *
   * @param sourceId the {@code sourceId} the source was registered with
   */
  @PostMapping("/{sourceId}:validate")
  public SourceValidationResult validate(@PathVariable String sourceId) {
    return sourceCatalogService.validate(sourceId);
  }
}

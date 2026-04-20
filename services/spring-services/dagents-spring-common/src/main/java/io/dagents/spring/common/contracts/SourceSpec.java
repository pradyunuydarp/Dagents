package io.dagents.spring.common.contracts;

import jakarta.validation.constraints.NotBlank;
import java.util.Map;

public record SourceSpec(
    @NotBlank String sourceId,
    @NotBlank String kind,
    String connectionRef,
    Map<String, Object> selection,
    String format,
    Map<String, String> schemaHint,
    SourceBatching batching,
    Map<String, Object> checkpoint,
    Map<String, Object> options
) {}

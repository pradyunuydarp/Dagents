package io.dagents.spring.common.contracts;

import java.util.List;

public record SourceValidationResult(
    boolean valid,
    List<String> errors,
    List<String> warnings
) {}

package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * The result of validating a source definition.
 *
 * @param valid {@code false} when there is at least one error
 * @param errors problems that make the definition unusable
 * @param warnings problems that do not; always empty in the control service
 */
public record SourceValidationResult(
    boolean valid,
    List<String> errors,
    List<String> warnings
) {}

package io.dagents.spring.common.contracts;

import jakarta.validation.constraints.NotBlank;
import java.util.Map;

/**
 * A data source definition in the control service's catalog. It is stored and returned as sent;
 * the service reads only {@code sourceId}, {@code kind} and {@code connectionRef}.
 *
 * @param sourceId the source's id, and the key it is stored under; must not be blank
 * @param kind the kind of source, such as {@code inline} or {@code postgres}; must not be blank.
 *     Any text is accepted
 * @param connectionRef the name of a connection; validation requires it unless {@code kind} is
 *     {@code inline}
 * @param selection what to read, such as a table or records; stored, not read
 * @param format the file format for file sources; stored, not read
 * @param schemaHint expected field types; stored, not read
 * @param batching how many records to read at a time; stored, not read
 * @param checkpoint where a later read should resume; stored, not read
 * @param options other settings for the adapter; stored, not read
 */
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

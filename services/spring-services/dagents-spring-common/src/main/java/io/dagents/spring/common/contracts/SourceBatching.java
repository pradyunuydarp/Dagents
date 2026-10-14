package io.dagents.spring.common.contracts;

/**
 * How many records to read at a time.
 *
 * @param batchSize records per batch; 0 when left out
 * @param maxRecords the most records to read, or null for no limit
 */
public record SourceBatching(
    int batchSize,
    Integer maxRecords
) {}

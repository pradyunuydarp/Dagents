package io.dagents.spring.common.contracts;

public record SourceBatching(
    int batchSize,
    Integer maxRecords
) {}

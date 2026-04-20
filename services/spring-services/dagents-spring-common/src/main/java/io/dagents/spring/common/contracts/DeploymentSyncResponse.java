package io.dagents.spring.common.contracts;

public record DeploymentSyncResponse(
    boolean upToDate,
    String planToken,
    String configDigest,
    String desiredBundleId,
    String desiredBundleVersion,
    String desiredBundleUri
) {}

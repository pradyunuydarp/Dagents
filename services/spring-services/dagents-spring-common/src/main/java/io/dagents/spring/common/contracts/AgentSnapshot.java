package io.dagents.spring.common.contracts;

import java.util.List;
import java.util.Map;

public record AgentSnapshot(
    AgentIdentity agent,
    Map<String, String> scope,
    String version,
    List<String> capabilities,
    String status,
    Long lastSeenAt,
    String lastHeartbeatStatus,
    String lastIngestionId,
    String desiredBundleId,
    String desiredBundleVersion,
    String currentBundleId,
    String currentBundleVersion
) {}

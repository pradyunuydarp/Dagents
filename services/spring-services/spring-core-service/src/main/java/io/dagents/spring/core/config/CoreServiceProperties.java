package io.dagents.spring.core.config;

import java.util.Objects;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "dagents.core")
public record CoreServiceProperties(
    String appName,
    String environment,
    String controlServiceUrl,
    String lmaUrl,
    String gmaUrl,
    String modelServiceUrl,
    String pipelineServiceUrl
) {
  public CoreServiceProperties {
    appName = appName == null ? "dagents-spring-core-service" : appName;
    environment = environment == null ? "development" : environment;
    controlServiceUrl = Objects.requireNonNull(controlServiceUrl, "dagents.core.control-service-url is required");
    lmaUrl = Objects.requireNonNull(lmaUrl, "dagents.core.lma-url is required");
    gmaUrl = Objects.requireNonNull(gmaUrl, "dagents.core.gma-url is required");
    modelServiceUrl = Objects.requireNonNull(modelServiceUrl, "dagents.core.model-service-url is required");
    pipelineServiceUrl = Objects.requireNonNull(pipelineServiceUrl, "dagents.core.pipeline-service-url is required");
  }
}

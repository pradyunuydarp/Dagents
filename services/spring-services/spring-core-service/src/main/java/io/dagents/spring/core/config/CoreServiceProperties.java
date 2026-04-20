package io.dagents.spring.core.config;

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
    controlServiceUrl = controlServiceUrl == null ? "http://spring-control-service:8050" : controlServiceUrl;
    lmaUrl = lmaUrl == null ? "http://lma:8010" : lmaUrl;
    gmaUrl = gmaUrl == null ? "http://gma:8020" : gmaUrl;
    modelServiceUrl = modelServiceUrl == null ? "http://model-service:8000" : modelServiceUrl;
    pipelineServiceUrl = pipelineServiceUrl == null ? "http://pipeline-service:8030" : pipelineServiceUrl;
  }
}

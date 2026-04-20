package io.dagents.spring.control.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "dagents.control")
public record ControlServiceProperties(
    String appName,
    String environment
) {
  public ControlServiceProperties {
    appName = appName == null ? "dagents-spring-control-service" : appName;
    environment = environment == null ? "development" : environment;
  }
}

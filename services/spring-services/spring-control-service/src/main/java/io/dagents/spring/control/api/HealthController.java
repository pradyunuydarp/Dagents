package io.dagents.spring.control.api;

import io.dagents.spring.common.contracts.HealthResponse;
import io.dagents.spring.control.config.ControlServiceProperties;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1")
public class HealthController {
  private final ControlServiceProperties properties;

  public HealthController(ControlServiceProperties properties) {
    this.properties = properties;
  }

  @GetMapping("/health")
  public HealthResponse health() {
    return new HealthResponse("ok", properties.appName(), properties.environment(), "http");
  }
}

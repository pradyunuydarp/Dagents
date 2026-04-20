package io.dagents.spring.control.config;

import io.dagents.spring.control.infrastructure.InMemoryAgentRegistryRepository;
import io.dagents.spring.control.infrastructure.InMemoryDeploymentRepository;
import io.dagents.spring.control.infrastructure.InMemorySourceRepository;
import io.dagents.spring.control.infrastructure.InMemoryTelemetryRepository;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(ControlServiceProperties.class)
public class ControlConfiguration {
  @Bean
  InMemoryAgentRegistryRepository agentRegistryRepository() {
    return new InMemoryAgentRegistryRepository();
  }

  @Bean
  InMemoryTelemetryRepository telemetryRepository() {
    return new InMemoryTelemetryRepository();
  }

  @Bean
  InMemoryDeploymentRepository deploymentRepository() {
    return new InMemoryDeploymentRepository();
  }

  @Bean
  InMemorySourceRepository sourceRepository() {
    return new InMemorySourceRepository();
  }
}

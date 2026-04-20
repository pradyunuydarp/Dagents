package io.dagents.spring.core.config;

import io.dagents.spring.core.application.InMemoryWorkloadPlanRepository;
import io.dagents.spring.core.compiler.JavaManifestCompiler;
import io.dagents.spring.core.compiler.ManifestCompilerGateway;
import io.dagents.spring.core.compiler.ProcessManifestCompilerGateway;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(CoreServiceProperties.class)
public class CoreConfiguration {
  @Bean
  InMemoryWorkloadPlanRepository workloadPlanRepository() {
    return new InMemoryWorkloadPlanRepository();
  }

  @Bean
  ManifestCompilerGateway manifestCompilerGateway() {
    return new ProcessManifestCompilerGateway(new JavaManifestCompiler(), System.getenv("DAGENTS_OCAML_MANIFEST_CLI"));
  }
}

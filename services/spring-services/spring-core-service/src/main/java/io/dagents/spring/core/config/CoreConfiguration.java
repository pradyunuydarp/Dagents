package io.dagents.spring.core.config;

import io.dagents.spring.core.compiler.JavaManifestCompiler;
import io.dagents.spring.core.compiler.ManifestCompilerGateway;
import io.dagents.spring.core.compiler.ProcessManifestCompilerGateway;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * The beans the component scan does not create.
 *
 * <p>{@code InMemoryWorkloadPlanRepository} is a {@code @Repository}, so the scan creates it. A
 * second {@code @Bean} for it here once stopped the service from starting.
 */
@Configuration
@EnableConfigurationProperties(CoreServiceProperties.class)
public class CoreConfiguration {
  @Bean
  ManifestCompilerGateway manifestCompilerGateway() {
    return new ProcessManifestCompilerGateway(new JavaManifestCompiler(), System.getenv("DAGENTS_OCAML_MANIFEST_CLI"));
  }
}

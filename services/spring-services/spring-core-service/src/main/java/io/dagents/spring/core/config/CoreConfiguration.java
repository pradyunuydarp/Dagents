package io.dagents.spring.core.config;

import io.dagents.spring.core.compiler.JavaManifestCompiler;
import io.dagents.spring.core.compiler.ManifestCompilerGateway;
import io.dagents.spring.core.compiler.ProcessManifestCompilerGateway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(CoreServiceProperties.class)
public class CoreConfiguration {
  @Bean
  ManifestCompilerGateway manifestCompilerGateway(@Value("${DAGENTS_OCAML_MANIFEST_CLI:}") String ocamlManifestCli) {
    return new ProcessManifestCompilerGateway(new JavaManifestCompiler(), ocamlManifestCli);
  }
}

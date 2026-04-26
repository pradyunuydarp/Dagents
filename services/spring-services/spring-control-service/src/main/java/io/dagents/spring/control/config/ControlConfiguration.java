package io.dagents.spring.control.config;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@EnableConfigurationProperties(ControlServiceProperties.class)
public class ControlConfiguration {}

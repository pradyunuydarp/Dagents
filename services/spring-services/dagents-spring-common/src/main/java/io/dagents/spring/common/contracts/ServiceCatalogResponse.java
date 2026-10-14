package io.dagents.spring.common.contracts;

import java.util.List;

/**
 * The services spring-core-service is configured with.
 *
 * @param services five services, always in this order: spring-control-service, lma, gma,
 *     model-service, pipeline-service
 */
public record ServiceCatalogResponse(List<ServiceDescriptor> services) {}

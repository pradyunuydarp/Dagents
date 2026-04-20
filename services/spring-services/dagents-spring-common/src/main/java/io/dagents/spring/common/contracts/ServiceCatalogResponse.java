package io.dagents.spring.common.contracts;

import java.util.List;

public record ServiceCatalogResponse(List<ServiceDescriptor> services) {}
